"""Frozen offline VWAP ablation; no registry, network or execution authority."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vnedge.data.vwap import vwap_from_sums
from vnedge.plan.cash_costs import fee_cash
from vnedge.plan.cost_model import CostModel
from vnedge.strategy.arm_evidence import FrozenPermissionSnapshot, assert_decision_row
from vnedge.strategy.base_strategy import SignalIntent, bind_signal_decision

HERE = Path(__file__).resolve().parent
START = pd.Timestamp('2026-09-01T00:00:00Z')
END = pd.Timestamp('2026-09-11T00:00:00Z')
STEP = pd.Timedelta(minutes=5)
VARIANTS = ('base', 'vwap', 'volume', 'both')
COST_HASH = 'e04d112e35708157a77fc5b5721e4e2c4b2c0ce6d05c71b65bff3bf443e7b13a'


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(name: str, path: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HERE.parent / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load(root: Path, symbol: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    ref = reference('vwap_minutes', 'burst_response_20260912/screen.py')
    minutes, manifest = ref.load_minutes(root, symbol)
    # Restore exact stored Decimals after the reused validator's float view.
    raw = pd.concat([pd.read_parquet(p['path']) for p in manifest['files']])
    raw = raw.set_index('open_time').reindex(minutes.index)
    for key in ('open', 'high', 'low', 'close', 'volume', 'quote_volume'):
        minutes[key] = raw[key]
    loader = reference('vwap_decisions', 'range_retest_20260912/replay.py')
    frame, files = loader.load_decisions(root, symbol)
    frame = frame.set_index('timestamp').reindex(pd.date_range(START, END, freq='5min', inclusive='left'))
    eligible, reasons = [], Counter()
    for ts, row in frame.iterrows():
        try:
            assert_decision_row(row.to_dict() | {'timestamp': ts}, timeframe='5m')
            if row.coverage_ok is not True and row.coverage_ok != True:
                raise ValueError('coverage_missing')
            children = minutes.reindex(pd.date_range(ts, periods=5, freq='min'))
            if not children.eligible.eq(True).all():
                raise ValueError('incomplete_minute_children')
            expected = dict(open=children.open.iloc[0], close=children.close.iloc[-1],
                            high=children.high.max(), low=children.low.min(),
                            volume=sum(children.volume, Decimal(0)),
                            quote_volume=sum(children.quote_volume, Decimal(0)))
            if any(Decimal(str(row[k])) != Decimal(str(v)) for k, v in expected.items()):
                raise ValueError('child_rollup_mismatch')
            if expected['volume'] <= 0 or expected['quote_volume'] <= 0:
                raise ValueError('nonpositive_exact_volume')
            eligible.append(True)
        except (ValueError, TypeError, AttributeError) as exc:
            eligible.append(False)
            reasons[str(exc)] += 1
    frame['eligible'] = eligible
    for item in manifest['files'] + files:
        if file_hash(Path(item['path'])) != item['sha256']:
            raise ValueError('input_changed_during_read')
    return frame, minutes, dict(minutes=manifest, decisions=files,
                               eligible_5m=sum(eligible), rejected=dict(reasons))


def prepare(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    vwaps, good = [], []
    day, quote, base, covered = None, Decimal(0), Decimal(0), False
    for ts, row in frame.iterrows():
        if ts.floor('D') != day:
            day, quote, base = ts.floor('D'), Decimal(0), Decimal(0)
            covered = ts == day
        covered = covered and bool(row.eligible)
        if covered:
            quote += Decimal(str(row.quote_volume))
            base += Decimal(str(row.volume))
        value = vwap_from_sums(quote, base) if covered else None
        vwaps.append(float(value) if value is not None else np.nan)
        good.append(covered)
    out['session_vwap'], out['session_ok'] = vwaps, good
    for key in ('open', 'high', 'low', 'close', 'quote_volume'):
        out[key] = out[key].astype(float)
    return out


def generate(frame: pd.DataFrame, symbol: str) -> tuple[list[dict], dict]:
    bars = prepare(frame)
    events, counts = [], Counter()
    reserved = START
    for i, (ts, row) in enumerate(bars.iterrows()):
        if not row.session_ok or i < 21 or not bars.session_ok.iloc[i-21:i+1].all():
            counts['session_coverage_or_warmup'] += 1
            continue
        if bars.index[i-21].floor('D') != ts.floor('D'):
            counts['session_warmup'] += 1
            continue
        if ts + STEP < reserved:
            counts['shared_episode_reservation'] += 1
            continue
        prior = bars.iloc[i-21:i]
        tr = np.maximum(prior.high.to_numpy()[1:]-prior.low.to_numpy()[1:],
                        np.maximum(abs(prior.high.to_numpy()[1:]-prior.close.to_numpy()[:-1]),
                                   abs(prior.low.to_numpy()[1:]-prior.close.to_numpy()[:-1])))
        atr = float(tr.mean())
        window = prior.iloc[-6:]
        low, high = float(window.low.min()), float(window.high.max())
        if atr <= 0 or high-low > 1.5*atr or row.close <= high:
            counts['no_consolidation_breakout'] += 1
            continue
        reserved = ts + STEP + pd.Timedelta(minutes=30)
        vwap_ok = bool((abs(window.close-window.session_vwap) <= .5*atr).all()
                       and row.session_vwap > bars.session_vwap.iloc[i-3]
                       and 0 < row.close-row.session_vwap <= atr)
        ratio = float(row.quote_volume/prior.quote_volume.iloc[-20:].median())
        volume_ok = ratio >= 1.5
        stop = low-.1*atr
        target = float(row.close + 2*(row.close-stop))
        movement = prior.close.iloc[-1]-prior.close.iloc[0]
        travel = abs(prior.close.diff()).sum()
        regime = ('up' if movement > 0 else 'down') if travel > 0 and abs(movement)/travel >= .30 else 'range'
        hashes = frame.loc[ts.floor('D'):ts, 'content_sha256'].tolist()
        episode = digest((symbol, ts.isoformat(), hashes, file_hash(HERE/'CONTRACT.md')))
        event = dict(episode_id=episode, decision_open=ts.isoformat(),
                     decision_close=(ts+STEP).isoformat(), stop_price=stop, target_price=target,
                     session_vwap=float(row.session_vwap), atr=atr, volume_ratio=ratio,
                     vwap_ok=vwap_ok, volume_ok=volume_ok, regime=regime,
                     session=f'{ts.hour//6*6:02d}-{ts.hour//6*6+6:02d}',
                     session_input_hash=digest(hashes), envelopes={})
        original = frame.iloc[i].to_dict() | {'timestamp': ts}
        for variant in VARIANTS:
            if not selected(event, variant):
                continue
            sid = f'vwap_consolidation_5m_{variant}_v1'
            reason = f'{sid} episode={episode}'
            permission = FrozenPermissionSnapshot(
                decision_bar=assert_decision_row(original, timeframe='5m'),
                context_bars=(), allow_long=True, allow_short=False,
                regime_state='not_applicable', direction='not_applicable',
                reason=reason, regime_version=sid)
            signal = bind_signal_decision(
                SignalIntent(side='long', stop_price=stop, take_profit_price=target,
                             reason=reason, permission_snapshot=permission),
                strategy_id=sid, symbol=symbol, timeframe='5m', decision_row=original,
                entry_clock='next_5m_open', require_canonical_truth=True,
                require_existing_snapshot=True)
            event['envelopes'][variant] = asdict(signal.decision_envelope)
        events.append(event)
    counts['baseline_arms'] = len(events)
    counts['session_covered_bars'] = int(bars.session_ok.sum())
    return events, dict(counts)


def selected(event: dict, variant: str) -> bool:
    return (variant not in ('vwap', 'both') or event['vwap_ok']) and (
        variant not in ('volume', 'both') or event['volume_ok'])


def cash_return(entry: float, exit_price: float, slip_bps: float = 3) -> float:
    buy = Decimal(str(entry))*(1+Decimal(str(slip_bps))/10000)
    sell = Decimal(str(exit_price))*(1-Decimal(str(slip_bps))/10000)
    fees = fee_cash(buy, '5.9') + fee_cash(sell, '5.9')
    return float((sell-buy-fees)/buy*10000)


def simulate(event: dict, minutes: pd.DataFrame) -> dict:
    at = pd.Timestamp(event['decision_close'])
    record = dict(event, net_bps=None, funding='unavailable', status='censored_missing_entry')
    if at not in minutes.index or not minutes.loc[at, 'eligible']:
        return record
    entry = float(minutes.loc[at, 'open'])
    stop, target = event['stop_price'], event['target_price']
    if not stop < entry*1.0003 < target:
        return dict(record, status='entry_rejected_geometry')
    for ts in pd.date_range(at, periods=30, freq='min'):
        if ts not in minutes.index or not minutes.loc[ts, 'eligible']:
            return dict(record, status='censored_missing_path', missing_at=ts.isoformat())
        row = minutes.loc[ts]
        op, high, low, close = (float(row[k]) for k in ('open', 'high', 'low', 'close'))
        price, reason = None, None
        if op <= stop:
            price, reason = op, 'stop_gap'
        elif op >= target:
            price, reason = target, 'target_gap_no_improvement'
        elif low <= stop:
            price, reason = stop, 'stop_tie' if high >= target else 'stop'
        elif high >= target:
            price, reason = target, 'target'
        elif ts == at + pd.Timedelta(minutes=29):
            price, reason = close, 'timeout'
        if price is not None:
            return dict(record, status='measured', entry_proxy=entry, exit_proxy=price,
                        exit_known_by=(ts+pd.Timedelta(minutes=1)).isoformat(), exit_reason=reason,
                        gross_bps=(price/entry-1)*10000,
                        net_before_funding_bps=cash_return(entry, price),
                        stress_before_funding_bps=cash_return(entry, price, 6))
    raise AssertionError('unreachable')


def bootstrap(events: list[dict]) -> np.ndarray:
    days = pd.date_range(START, END, freq='D', inclusive='left')
    sums = np.array([sum(e['net_before_funding_bps'] for e in events
                         if pd.Timestamp(e['decision_open']).floor('D') == d) for d in days])
    counts = np.array([sum(pd.Timestamp(e['decision_open']).floor('D') == d for e in events) for d in days])
    draws = np.random.default_rng(20260914).integers(0, len(days), (2000, len(days)))
    denominator = counts[draws].sum(axis=1)
    return np.divide(sums[draws].sum(axis=1), denominator,
                     out=np.full(2000, np.nan), where=denominator > 0)


def summary(events: list[dict]) -> dict:
    done = [e for e in events if e['status'] == 'measured']
    counts = dict(Counter(e['status'] for e in events))
    if not done:
        return dict(n=0, counts=counts, verdict='INSUFFICIENT', net_bps=None)
    net = np.array([e['net_before_funding_bps'] for e in done])
    curve = np.r_[0, net.cumsum()]
    boot = bootstrap(done)
    return dict(n=len(done), counts=counts, gross_mean_bps=float(np.mean([e['gross_bps'] for e in done])),
                net_before_funding_mean_bps=float(net.mean()), net_bps=None,
                profit_factor=float(net[net>0].sum()/-net[net<0].sum()) if (net<0).any() else None,
                drawdown_unit_notional_bps=float((np.maximum.accumulate(curve)-curve).max()),
                descriptive_day_bootstrap_95_bps=np.nanquantile(boot, [.025,.975]).tolist(),
                stress_before_funding_mean_bps=float(np.mean([e['stress_before_funding_bps'] for e in done])),
                verdict='INSUFFICIENT' if len(done)<30 else 'EXPLORATORY_ONLY')


def run(root: Path, output: Path) -> None:
    cost = CostModel.for_profile('delta_scalp_v2')
    if cost.config_sha256 != COST_HASH:
        raise ValueError('cost_contract_drift')
    output.mkdir(parents=True, exist_ok=False)
    report = dict(contract_sha256=file_hash(HERE/'CONTRACT.md'),
                  source_sha256=file_hash(Path(__file__)),
                  git_revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
                  cost_profile_id=cost.profile, cost_config_sha256=cost.config_sha256,
                  nominal_booked_round_bps=cost.round_trip_bps(include_safety=False),
                  gate_wall_bps=cost.round_trip_bps(include_safety=True),
                  fill_assumption='canonical_1m_next_5m_open_stop_first_with_3bps_adverse_each_leg',
                  funding='unavailable', untouched_oos=False, can_trade=False,
                  can_promote=False, performance_eligible=False, symbols={})
    for symbol in ('BTCUSD','ETHUSD'):
        frame, minutes, inputs = load(root, symbol)
        arms, diagnostics = generate(frame, symbol)
        events = [simulate(e, minutes) for e in arms]
        cells = {}
        for variant in VARIANTS:
            chosen = [e for e in events if selected(e, variant)]
            cell = dict(summary=summary(chosen))
            cell['strata'] = {key: {value: summary([e for e in chosen if e[key]==value])
                                    for value in values}
                              for key,values in [('session',('00-06','06-12','12-18','18-24')),
                                                 ('regime',('up','down','range'))]}
            cell['chronological_not_oos'] = {
                'first_seven_days': summary([e for e in chosen if pd.Timestamp(e['decision_open']) < START+pd.Timedelta(days=7)]),
                'last_three_days': summary([e for e in chosen if pd.Timestamp(e['decision_open']) >= START+pd.Timedelta(days=7)])}
            rejected = [e for e in events if not selected(e,variant) and e['status']=='measured']
            accepted = [e for e in chosen if e['status']=='measured']
            if accepted and rejected:
                delta = bootstrap(accepted)-bootstrap(rejected)
                cell['selected_minus_rejected_95_bps'] = np.nanquantile(delta,[.025,.975]).tolist()
            cells[variant] = cell
        report['symbols'][symbol] = dict(inputs=inputs, diagnostics=diagnostics, cells=cells)
        with (output/f'{symbol}.events.json').open('x') as stream:
            json.dump(events, stream, indent=2, default=str, allow_nan=False)
        report['symbols'][symbol]['events_sha256'] = file_hash(output/f'{symbol}.events.json')
    with (output/'results.json').open('x') as stream:
        json.dump(report,stream,indent=2,default=str,allow_nan=False)
    print(json.dumps({s:{k:v['summary'] for k,v in r['cells'].items()} for s,r in report['symbols'].items()},indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=HERE/'attempt_01')
    args = parser.parse_args()
    run(args.input,args.output)
