"""Bounded, preregistered VWAP pullback matrix. Research only."""
from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import subprocess
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vnedge.data.candles import Candle
from vnedge.data.swings import SwingKind, detect_swings
from vnedge.plan.cost_model import CostModel
from vnedge.strategy.arm_evidence import FrozenPermissionSnapshot, assert_decision_row
from vnedge.strategy.base_strategy import SignalIntent, bind_signal_decision

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('frozen_vwap_reference', HERE.parent/'vwap_consolidation_20260914/replay.py')
REF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REF)
START, END, STEP = REF.START, REF.END, REF.STEP
TIMINGS = ('immediate', 'delay3', 'higher_low3')
VOLUMES = ('none', 'contraction', 'expansion', 'both')
BANDS = ('wide', 'tight')
EXITS = ('short', 'standard')


def cells() -> list[tuple[str, str, str, str]]:
    return list(itertools.product(TIMINGS, VOLUMES, BANDS, EXITS))


def key(cell: tuple[str, str, str, str]) -> str:
    return '_'.join(cell)


def origins(frame: pd.DataFrame, symbol: str) -> tuple[list[dict], dict]:
    bars = REF.prepare(frame)
    result, counts = [], Counter()
    reserved = START
    contract_hash = REF.file_hash(HERE/'CONTRACT.md')
    for i, (ts, row) in enumerate(bars.iterrows()):
        if i < 21 or not bars.session_ok.iloc[i-21:i+1].all() or bars.index[i-21].floor('D') != ts.floor('D'):
            counts['session_coverage_or_warmup'] += 1
            continue
        if ts+STEP < reserved:
            counts['shared_episode_reservation'] += 1
            continue
        prior = bars.iloc[i-21:i]
        tr = np.maximum(prior.high.to_numpy()[1:]-prior.low.to_numpy()[1:],
                        np.maximum(abs(prior.high.to_numpy()[1:]-prior.close.to_numpy()[:-1]),
                                   abs(prior.low.to_numpy()[1:]-prior.close.to_numpy()[:-1])))
        atr = float(tr.mean())
        if atr <= 0:
            counts['invalid_atr'] += 1
            continue
        established, pullback = bars.iloc[i-5:i-2], bars.iloc[i-2:i]
        distances = pullback.low-pullback.session_vwap
        span = row.high-row.low
        gates = {
            'not_established_above': not (established.close > established.session_vwap).all(),
            'no_pullback': not (pullback.close.iloc[-1] < established.close.iloc[-1] and (pullback.close < pullback.open).any()),
            'outside_wide_band': not (distances.min() <= .5*atr and distances.min() >= -.25*atr),
            'no_strong_recovery': not (span > 0 and row.close-row.open >= .5*span
                                      and row.close >= row.low+.75*span
                                      and row.close > pullback.high.iloc[-1]
                                      and row.close > row.session_vwap),
            'vwap_not_rising': not row.session_vwap > bars.session_vwap.iloc[i-3],
        }
        failed = [k for k,v in gates.items() if v]
        counts.update(failed)
        if failed:
            continue
        reserved = ts+STEP+pd.Timedelta(minutes=45)
        contraction = float(pullback.quote_volume.mean()/bars.quote_volume.iloc[i-12:i-2].median())
        expansion = float(row.quote_volume/prior.quote_volume.iloc[-20:].median())
        change = prior.close.iloc[-1]-prior.close.iloc[0]
        travel = abs(prior.close.diff()).sum()
        regime = ('up' if change > 0 else 'down') if travel > 0 and abs(change)/travel >= .30 else 'range'
        hashes = frame.loc[ts.floor('D'):ts, 'content_sha256'].tolist()
        result.append({
            'episode_id': REF.digest((symbol, contract_hash, ts, hashes)),
            'origin_index': i, 'origin_open': ts.isoformat(), 'origin_close': (ts+STEP).isoformat(),
            'origin_hash': str(frame.iloc[i].content_sha256), 'session_input_hash': REF.digest(hashes),
            'atr': atr, 'stop_price': float(min(pullback.low.min(),row.low)-.1*atr),
            'tight_ok': bool(distances.min() <= .25*atr and distances.min() >= -.1*atr),
            'contraction_ratio': contraction, 'expansion_ratio': expansion,
            'contraction_ok': contraction <= .75, 'expansion_ok': expansion >= 1.5,
            'regime': regime, 'session': f'{ts.hour//6*6:02d}-{ts.hour//6*6+6:02d}',
        })
    counts['common_origins'] = len(result)
    counts['session_covered_bars'] = int(bars.session_ok.sum())
    return result, dict(counts)


def pivot_proof(frame: pd.DataFrame, origin_index: int, arm_index: int, symbol: str) -> list[dict]:
    """Only a prefix ending at the actual delayed ARM is visible."""
    day = frame.index[arm_index].floor('D')
    prefix = frame.iloc[:arm_index+1].loc[day:]
    if not prefix.eligible.all():
        return []
    candles = [Candle(symbol=symbol,timeframe='5m',open_time=ts.to_pydatetime(),
                      close_time=(ts+STEP).to_pydatetime(),is_closed=True,
                      **{k: Decimal(str(row[k])) for k in ('open','high','low','close','volume','quote_volume')},
                      trade_count=int(row.trade_count)) for ts,row in prefix.iterrows()]
    lows = [a for a in detect_swings(candles) if a.kind == SwingKind.LOW]
    if len(lows) < 2:
        return []
    first, second = lows[-2:]
    permitted = {frame.index[origin_index-2], frame.index[origin_index-1]}
    if second.anchor_time not in permitted or second.anchor_price <= first.anchor_price:
        return []
    arm_close = frame.index[arm_index]+STEP
    if not all(a.visible_at(arm_close.to_pydatetime()) for a in (first, second)):
        raise AssertionError('future pivot leak')
    return [dict(anchor_open=a.anchor_time.isoformat(),confirmed_at=a.confirmed_at.isoformat(),
                 price=str(a.anchor_price),content_sha256=str(frame.loc[a.anchor_time,'content_sha256']))
            for a in (first,second)]


def arm(origin: dict, cell: tuple[str,str,str,str], frame: pd.DataFrame,
        prepared: pd.DataFrame, symbol: str) -> dict:
    timing, volume, band, exit_plan = cell
    sid = f'vwap_pullback_5m_{key(cell)}_v1'
    base = {**origin,'strategy_id':sid,'cell':key(cell),'status':'filtered',
            'net_bps':None,'funding':'unavailable','can_trade':False,'can_promote':False}
    failed = []
    if band == 'tight' and not origin['tight_ok']:
        failed.append('outside_tight_band')
    if volume in ('contraction','both') and not origin['contraction_ok']:
        failed.append('volume_not_contracted')
    if volume in ('expansion','both') and not origin['expansion_ok']:
        failed.append('volume_not_expanded')
    if failed:
        return {**base,'failed':failed}
    i = origin['origin_index']
    j = i + (0 if timing == 'immediate' else 3)
    if j >= len(frame) or not prepared.session_ok.iloc[i:j+1].all():
        return {**base,'status':'censored_missing_decision'}
    ts = frame.index[j]
    if ts.floor('D') != frame.index[i].floor('D'):
        return {**base,'status':'rejected_session_changed'}
    if j > i and prepared.low.iloc[i+1:j+1].min() <= origin['stop_price']:
        return {**base,'status':'rejected_stop_during_delay'}
    row = prepared.iloc[j]
    if row.close <= row.session_vwap:
        return {**base,'status':'rejected_permission_lost'}
    pivots = pivot_proof(frame,i,j,symbol) if timing == 'higher_low3' else []
    if timing == 'higher_low3' and not pivots:
        return {**base,'status':'rejected_higher_low_unconfirmed'}
    stop = origin['stop_price']
    reward, hold = (1.5,15) if exit_plan == 'short' else (2.,30)
    if row.close <= stop:
        return {**base,'status':'rejected_stop_geometry'}
    target = float(row.close+reward*(row.close-stop))
    proof_hash = REF.digest((origin['episode_id'],cell,pivots,
                            frame.loc[frame.index[i]:ts,'content_sha256'].tolist()))
    reason = f'{sid} evidence={proof_hash}'
    original = frame.iloc[j].to_dict() | {'timestamp':ts}
    permission = FrozenPermissionSnapshot(
        decision_bar=assert_decision_row(original,timeframe='5m'),context_bars=(),
        allow_long=True,allow_short=False,regime_state='not_applicable',direction='not_applicable',
        reason=reason,regime_version=sid)
    signal = bind_signal_decision(
        SignalIntent(side='long',stop_price=stop,take_profit_price=target,reason=reason,permission_snapshot=permission),
        strategy_id=sid,symbol=symbol,timeframe='5m',decision_row=original,entry_clock='next_5m_open',
        require_canonical_truth=True,require_existing_snapshot=True)
    return {**base,'status':'armed','decision_open':ts.isoformat(),'decision_close':(ts+STEP).isoformat(),
            'target_price':target,'hold_minutes':hold,'entry_vwap':float(row.session_vwap),
            'pivot_proof':pivots,'envelope':asdict(signal.decision_envelope),'proof_hash':proof_hash}


def simulate(event: dict, minutes: pd.DataFrame) -> dict:
    if event['status'] != 'armed':
        return event
    at = pd.Timestamp(event['decision_close'])
    if at not in minutes.index or not minutes.loc[at,'eligible']:
        return {**event,'status':'censored_missing_entry'}
    entry = float(minutes.loc[at,'open'])
    filled = entry*1.0003
    if not event['stop_price'] < filled < event['target_price']:
        return {**event,'status':'rejected_entry_geometry'}
    if not 0 < filled-event['entry_vwap'] <= event['atr']:
        return {**event,'status':'rejected_entry_extension'}
    stop, target = event['stop_price'],event['target_price']
    for ts in pd.date_range(at, periods=event['hold_minutes'],freq='min'):
        if ts not in minutes.index or not minutes.loc[ts,'eligible']:
            return {**event,'status':'censored_missing_path','missing_at':ts.isoformat()}
        row = minutes.loc[ts]
        op,high,low,close = (float(row[k]) for k in ('open','high','low','close'))
        price,reason = None,None
        if op <= stop:
            price,reason = op,'stop_gap'
        elif op >= target:
            price,reason = target,'target_gap_no_improvement'
        elif low <= stop:
            price,reason = stop,'stop_tie' if high >= target else 'stop'
        elif high >= target:
            price,reason = target,'target'
        elif ts == at+pd.Timedelta(minutes=event['hold_minutes']-1):
            price,reason = close,'timeout'
        if price is not None:
            return {**event,'status':'measured','entry_proxy':entry,'exit_proxy':price,'exit_reason':reason,
                    'exit_known_by':(ts+pd.Timedelta(minutes=1)).isoformat(),
                    'gross_bps':(price/entry-1)*10000,
                    'net_before_funding_bps':REF.cash_return(entry,price),
                    'stress_before_funding_bps':REF.cash_return(entry,price,6)}
    raise AssertionError('unreachable')


def summarize(events: list[dict]) -> dict:
    measured = [e for e in events if e['status']=='measured']
    # Unmeasured rows have origin timestamps but may never have an ARM.
    result = REF.summary(measured)
    result['counts'] = dict(Counter(e['status'] for e in events))
    active_days = len({e['decision_open'][:10] for e in measured})
    result['active_days'] = active_days
    if len(measured)<30 or active_days<5:
        result['descriptive_day_bootstrap_95_bps'] = None
        result['uncertainty_status'] = 'insufficient_trades_or_days'
    else:
        result['uncertainty_status'] = 'descriptive_not_multiple_testing_adjusted'
    return result


def comparisons(records: dict[str,list[dict]]) -> dict:
    result = {}
    for volume,band,exit_plan in itertools.product(VOLUMES,BANDS,EXITS):
        for left,right in (('immediate','delay3'),('delay3','higher_low3')):
            a,b = (records[key((t,volume,band,exit_plan))] for t in (left,right))
            amap,bmap = ({e['episode_id']:e for e in es if e['status']=='measured'} for es in (a,b))
            common = sorted(amap.keys() & bmap.keys())
            delta = [bmap[e]['net_before_funding_bps']-amap[e]['net_before_funding_bps'] for e in common]
            result[f'{left}_to_{right}_{volume}_{band}_{exit_plan}'] = {
                'matched_n':len(common),'left_only_n':len(amap.keys()-bmap.keys()),
                'right_only_n':len(bmap.keys()-amap.keys()),
                'right_minus_left_mean_before_funding_bps':float(np.mean(delta)) if delta else None,
                'note':'Matched available episodes only; excluded counts retained, no causal alpha claim'}
    return result


def run(root: Path, output: Path) -> None:
    cost = CostModel.for_profile('delta_scalp_v2')
    if cost.config_sha256 != REF.COST_HASH:
        raise ValueError('cost_drift')
    output.mkdir(parents=True,exist_ok=False)
    sources = [HERE/'CONTRACT.md',Path(__file__),Path(REF.__file__),
               HERE.parent/'burst_response_20260912/screen.py',
               HERE.parent/'range_retest_20260912/replay.py']
    sources += [HERE.parents[2]/'src/vnedge'/p for p in ('data/swings.py','data/bar_identity.py',
                'data/vwap.py','strategy/arm_evidence.py','strategy/base_strategy.py',
                'execution/evidence.py','plan/cost_model.py','plan/cash_costs.py')]
    result = {'source_hashes':{str(p.resolve()):REF.file_hash(p) for p in sources},
              'git_revision':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
              'trial_cells':96,'unique_strategy_revisions':48,'funding':'unavailable',
              'cost_profile_id':cost.profile,'cost_config_sha256':cost.config_sha256,
              'nominal_booked_round_bps':cost.round_trip_bps(include_safety=False),
              'can_trade':False,'can_promote':False,'performance_eligible':False,
              'untouched_oos':False,'symbols':{}}
    for symbol in ('BTCUSD','ETHUSD'):
        frame,minutes,inputs = REF.load(root,symbol)
        prepared = REF.prepare(frame)
        opportunities,diagnostics = origins(frame,symbol)
        records,results = {},{}
        for cell in cells():
            name = key(cell)
            events = [simulate(arm(o,cell,frame,prepared,symbol),minutes) for o in opportunities]
            records[name] = events
            results[name] = {'summary':summarize(events),
                'chronological_not_oos':{
                    'first_seven_days':summarize([e for e in events if pd.Timestamp(e['origin_open']) < START+pd.Timedelta(days=7)]),
                    'last_three_days':summarize([e for e in events if pd.Timestamp(e['origin_open']) >= START+pd.Timedelta(days=7)])},
                'strata':{k:{v:summarize([e for e in events if e[k]==v]) for v in values}
                          for k,values in [('session',('00-06','06-12','12-18','18-24')),('regime',('up','down','range'))]}}
        path = output/f'{symbol}.events.json'
        with path.open('x') as stream:
            json.dump(records,stream,indent=2,default=str,allow_nan=False)
        result['symbols'][symbol] = {'inputs':inputs,'diagnostics':diagnostics,'cells':results,
                                    'comparisons':comparisons(records),'events_sha256':REF.file_hash(path)}
    with (output/'results.json').open('x') as stream:
        json.dump(result,stream,indent=2,allow_nan=False)
    print(json.dumps({s:{'diagnostics':r['diagnostics'],'cells':{k:v['summary'] for k,v in r['cells'].items()}}
                      for s,r in result['symbols'].items()},indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=HERE/'attempt_01')
    args = parser.parse_args()
    run(args.input,args.output)
