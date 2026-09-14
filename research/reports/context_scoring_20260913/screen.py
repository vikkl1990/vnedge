"""Frozen exploratory scoring study; no strategy registration or execution."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vnedge.data.swings import detect_swings
from vnedge.plan.cost_model import CostModel

HERE = Path(__file__).resolve().parent
START = pd.Timestamp('2026-09-01T00:00:00Z')
SPLIT = pd.Timestamp('2026-09-06T00:00:00Z')
END = pd.Timestamp('2026-09-11T00:00:00Z')
STEP = pd.Timedelta(minutes=5)
GROUPS = ('structure', 'location', 'momentum', 'candle', 'participation')
FAMILIES = ('breakout20', 'wick_reversal20', 'ema20_reclaim', 'bos3', 'choch3')
VARIANTS = ('baseline', 'balanced60', 'regime60', 'session_regime60') + tuple(
    'without_' + g for g in GROUPS)
SAFETY = dict(can_trade=False, can_promote=False, performance_eligible=False)


def ref(name: str, sibling: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HERE.parent / sibling / 'screen.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, allow_nan=False).encode()).hexdigest()


def wilder(close: pd.Series, n: int = 14) -> pd.Series:
    out = pd.Series(np.nan, index=close.index)
    if len(close) <= n:
        return out
    delta = close.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    ag, al = gain.iloc[1:n+1].mean(), loss.iloc[1:n+1].mean()
    for i in range(n, len(close)):
        if i > n:
            ag += (gain.iloc[i]-ag)/n
            al += (loss.iloc[i]-al)/n
        out.iloc[i] = 50. if ag == al == 0 else (100. if al == 0 else 100-100/(1+ag/al))
    return out


def indicators(bars: pd.DataFrame) -> pd.DataFrame:
    """All recurrences restart at a bad calendar slot, never bridge a gap."""
    result = bars.copy(deep=True)
    names = ('ema20', 'ema50', 'atr20', 'hist', 'rsi', 'stoch', 'ha_body', 'volume_ratio')
    for name in names:
        result[name] = np.nan
    segment = (~bars.eligible).cumsum()
    for _, part in bars.loc[bars.eligible].groupby(segment[bars.eligible]):
        close = part.close
        result.loc[part.index, 'ema20'] = close.ewm(span=20, adjust=False, min_periods=20).mean()
        result.loc[part.index, 'ema50'] = close.ewm(span=50, adjust=False, min_periods=50).mean()
        tr = pd.concat([part.high-part.low, (part.high-close.shift()).abs(),
                        (part.low-close.shift()).abs()], axis=1).max(axis=1)
        result.loc[part.index, 'atr20'] = tr.rolling(20).mean()
        line = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(
            span=26, adjust=False, min_periods=26).mean()
        result.loc[part.index, 'hist'] = line-line.ewm(span=9, adjust=False, min_periods=9).mean()
        result.loc[part.index, 'rsi'] = wilder(close)
        low, high = part.low.rolling(14).min(), part.high.rolling(14).max()
        result.loc[part.index, 'stoch'] = 100*(close-low)/(high-low).replace(0, np.nan)
        result.loc[part.index, 'volume_ratio'] = part.quote_volume / part.quote_volume.shift().rolling(20).median()
        hac = (part.open+part.high+part.low+part.close)/4
        hao = float((part.open.iloc[0]+part.close.iloc[0])/2)
        for j, ts in enumerate(part.index):
            if j:
                hao = (hao+float(hac.iloc[j-1]))/2
            result.loc[ts, 'ha_body'] = hac.loc[ts]-hao
    result['warm'] = bars.eligible.rolling(50).sum().eq(50)
    return result


def hourly_context(bars: pd.DataFrame) -> pd.DataFrame:
    groups = bars.resample('1h', closed='left', label='left')
    h = groups.agg({'close': 'last', 'eligible': 'sum'})
    h['eligible'] = h.eligible.eq(12) & groups.size().eq(12)
    h['regime'] = 'unknown'
    h['ema20'], h['er20'] = np.nan, np.nan
    segments = (~h.eligible).cumsum()
    for _, part in h.loc[h.eligible].groupby(segments[h.eligible]):
        close = part.close
        ema = close.ewm(span=20, adjust=False, min_periods=20).mean()
        denominator = close.diff().abs().rolling(20).sum()
        er = close.diff(20).abs()/denominator.replace(0, np.nan)
        er = er.mask(denominator.eq(0), 0.)
        h.loc[part.index, 'ema20'], h.loc[part.index, 'er20'] = ema, er
        for ts in part.index:
            if pd.isna(er.loc[ts]):
                continue
            regime = 'range'
            if er.loc[ts] >= .3 and close.loc[ts] != ema.loc[ts]:
                regime = 'up' if close.loc[ts] > ema.loc[ts] else 'down'
            h.loc[ts, 'regime'] = regime
    return h


def structure(anchors: dict[str, list[dict]]) -> int:
    lo, hi = anchors['swing_low'], anchors['swing_high']
    if min(len(lo), len(hi)) < 2:
        return 0
    if lo[-1]['price'] > lo[-2]['price'] and hi[-1]['price'] > hi[-2]['price']:
        return 1
    if lo[-1]['price'] < lo[-2]['price'] and hi[-1]['price'] < hi[-2]['price']:
        return -1
    return 0


def event_features(b: pd.DataFrame, i: int, side: int, family: str,
                   anchors: dict[str, list[dict]], gaps: dict[int, dict]) -> dict[str, dict]:
    r, p = b.iloc[i], b.iloc[i-1]
    prior = b.iloc[i-20:i]
    level = float(prior.high.max() if side == 1 else prior.low.min())
    # Reversal touches opposite range edge; breakouts/reclaims touch broken edge.
    if family == 'wick_reversal20':
        level = float(prior.low.min() if side == 1 else prior.high.max())
    touch = lambda x: bool(r.low-.25*r.atr20 <= x <= r.high+.25*r.atr20 and side*(r.close-x) > 0)
    lo, hi = anchors['swing_low'], anchors['swing_high']
    fib: bool | None = None
    if lo and hi and hi[-1]['price'] > lo[-1]['price']:
        width = hi[-1]['price']-lo[-1]['price']
        ordered = hi[-1]['index'] > lo[-1]['index'] if side == 1 else lo[-1]['index'] > hi[-1]['index']
        retrace = (hi[-1]['price']-r.close)/width if side == 1 else (r.close-lo[-1]['price'])/width
        fib = bool(ordered and .382 <= retrace <= .618)
    same = lo if side == 1 else hi
    trendline: bool | None = None
    if len(same) >= 2:
        a, z = same[-2:]
        slope = (z['price']-a['price'])/(z['index']-a['index'])
        projected = z['price']+slope*(i-z['index'])
        trendline = bool(side*slope > 0 and touch(projected))
    divergence: bool | None = None
    if len(same) >= 2 and all(math.isfinite(a['rsi']) for a in same[-2:]):
        a, z = same[-2:]
        pdiff, rdiff = side*(z['price']-a['price']), side*(z['rsi']-a['rsi'])
        divergence = bool(z['confirm_index'] == i and (
            (pdiff < 0 < rdiff) if family == 'wick_reversal20' else (pdiff > 0 > rdiff)))
    gap = gaps.get(side)
    fvg = None if gap is None else bool(i > gap['created'] and r.low <= gap['high'] and
        r.high >= gap['low'] and side*(r.close-(gap['high'] if side == 1 else gap['low'])) > 0)
    width = r.high-r.low
    outer = width > 0 and ((r.close-r.low)/width >= .6 if side == 1 else (r.high-r.close)/width >= .6)
    features = {
        'structure': {'swing_pair': None if min(len(lo), len(hi)) < 2 else structure(anchors) == side},
        'location': {'range_touch': touch(level), 'ema_touch': touch(r.ema20),
                     'fib_retracement': fib, 'fvg_retest': fvg, 'trendline_touch': trendline},
        'momentum': {'macd_hist': side*r['hist'] > 0,
                     'rsi_zone': 50 <= r.rsi <= 70 if side == 1 else 30 <= r.rsi <= 50,
                     'stoch_zone': None if not math.isfinite(r.stoch) else (
                         50 <= r.stoch <= 80 if side == 1 else 20 <= r.stoch <= 50),
                     'rsi_divergence': divergence},
        'candle': {'direction_location': side*(r.close-r.open) > 0 and outer,
                   'engulfing': side*(p.close-p.open) < 0 and side*(r.close-r.open) > 0 and
                   min(r.open, r.close) <= min(p.open, p.close) and max(r.open, r.close) >= max(p.open, p.close),
                   'heikin_ashi_direction': side*r.ha_body > 0},
        'participation': {'notional_burst': r.volume_ratio >= 1.5},
    }
    return {g: {k: None if v is None else bool(v) for k, v in f.items()} for g, f in features.items()}


def scores(features: dict[str, dict], regime: str) -> tuple[dict, dict, dict]:
    group_scores, coverage = {}, {}
    for g, members in features.items():
        values = [float(v) for v in members.values() if v is not None]
        group_scores[g] = float(np.mean(values)) if values else 0.
        coverage[g] = len(values)/len(members)
    weights = (10, 35, 15, 25, 15) if regime == 'range' else (30, 20, 25, 10, 15)
    calculated = {'balanced60': 20*sum(group_scores.values()),
                  'regime60': sum(w*group_scores[g] for w, g in zip(weights, GROUPS))}
    calculated.update({'without_'+g: 25*sum(v for k, v in group_scores.items() if k != g) for g in GROUPS})
    return calculated, group_scores, coverage


def generate(bars: pd.DataFrame, symbol: str) -> tuple[list[dict], dict]:
    if not bars.index.is_unique or not bars.index.is_monotonic_increasing:
        raise ValueError('calendar_identity_invalid')
    if len(bars) > 1 and not bars.index.to_series().diff().iloc[1:].eq(STEP).all():
        raise ValueError('calendar_slots_must_include_gaps')
    if bars.loc[bars.eligible, 'is_closed'].ne(True).any():
        raise ValueError('eligible_forming_bar')
    helper = ref('context_swings_helper', 'gann_slope_20260913')
    b, h = indicators(bars), hourly_context(bars)
    events, counts = [], Counter()
    anchors: dict[str, list[dict]] = {'swing_low': [], 'swing_high': []}
    gaps: dict[int, dict] = {}
    used: set[tuple] = set()
    for i, (ts, r) in enumerate(b.iterrows()):
        if not r.eligible:
            anchors = {'swing_low': [], 'swing_high': []}
            gaps.clear()
            counts['invalid_calendar_slot'] += 1
            continue
        old_trend = structure(anchors)
        old_anchors = {k: v[-1].copy() if v else None for k, v in anchors.items()}
        if i >= 6 and b.eligible.iloc[i-6:i+1].all():
            window = b.iloc[i-6:i+1]
            for a in detect_swings([helper.candle(symbol, t, row) for t, row in window.iterrows()]):
                pi = i-3
                anchors[a.kind.value].append(dict(index=pi, confirm_index=i, price=float(a.anchor_price),
                    open_time=b.index[pi].isoformat(), confirmed_at=(ts+STEP).isoformat(),
                    bar_hash=str(b.iloc[pi].content_sha256), rsi=float(b.iloc[pi].rsi)))
                anchors[a.kind.value] = anchors[a.kind.value][-2:]
        for side, gap in list(gaps.items()):
            if i-gap['created'] > 12 or (r.close < gap['low'] if side == 1 else r.close > gap['high']):
                del gaps[side]
        if i >= 2 and b.eligible.iloc[i-2:i+1].all():
            if r.low > b.high.iloc[i-2]:
                gaps[1] = dict(low=float(b.high.iloc[i-2]), high=float(r.low), created=i)
            if r.high < b.low.iloc[i-2]:
                gaps[-1] = dict(low=float(r.high), high=float(b.low.iloc[i-2]), created=i)
        if not r.warm or not math.isfinite(r.atr20) or r.atr20 <= 0:
            counts['warmup_or_gap'] += 1
            continue
        p, prior = b.iloc[i-1], b.iloc[i-20:i]
        rh, rl = float(prior.high.max()), float(prior.low.min())
        width = r.high-r.low
        hits: dict[str, list[int]] = {f: [] for f in FAMILIES}
        for side in (1, -1):
            level = rh if side == 1 else rl
            if side*(p.close-level) <= 0 < side*(r.close-level):
                hits['breakout20'].append(side)
            rev = (r.low < rl < r.close if side == 1 else r.high > rh > r.close)
            outer = width > 0 and ((r.close-r.low)/width >= .6 if side == 1 else (r.high-r.close)/width >= .6)
            if rev and outer and side*(r.close-r.open) > 0:
                hits['wick_reversal20'].append(side)
            if side*(p.close-p.ema20) <= 0 < side*(r.close-r.ema20):
                hits['ema20_reclaim'].append(side)
            anchor = old_anchors['swing_high' if side == 1 else 'swing_low']
            if anchor and old_trend and side*(p.close-anchor['price']) <= 0 < side*(r.close-anchor['price']):
                key = (side, anchor['bar_hash'])
                if key not in used:
                    used.add(key)
                    hits['bos3' if old_trend == side else 'choch3'].append(side)
        close = ts+STEP
        parent = close.floor('1h')-pd.Timedelta(hours=1)
        regime = str(h.loc[parent, 'regime']) if parent in h.index else 'unknown'
        # Hash the exact complete-child context prefix used by EMA/ER, including
        # the recurrence seed. This is a derived research ref, never a fake lake hash.
        context_hash = None
        if regime != 'unknown':
            context_start = parent
            while context_start-pd.Timedelta(hours=1) in h.index and bool(
                    h.loc[context_start-pd.Timedelta(hours=1), 'eligible']):
                context_start -= pd.Timedelta(hours=1)
            child_hashes = b.loc[(b.index >= context_start) &
                                (b.index < parent+pd.Timedelta(hours=1)), 'content_sha256']
            context_hash = digest(child_hashes.tolist())
        session = f'UTC_{close.hour//6*6:02d}_{close.hour//6*6+6:02d}'
        for family, sides in hits.items():
            if len(sides) != 1:
                continue
            side = sides[0]
            features = event_features(b, i, side, family, anchors, gaps)
            calculated, group_scores, coverage = scores(features, regime)
            # Nonfinite RSI during anchor warmup is represented as null in evidence.
            anchor_evidence = {k: [dict(a, rsi=a['rsi'] if math.isfinite(a['rsi']) else None) for a in v]
                               for k, v in anchors.items()}
            event = dict(symbol=symbol, family=family, strategy_id=f'context_score_{family}_5m_v1',
                         side=side, decision_open=ts.isoformat(), decision_close=close.isoformat(),
                         decision_hash=str(r.content_sha256), regime=regime, session=session,
                         context_open=parent.isoformat() if regime != 'unknown' else None,
                         context_prefix_sha256=context_hash,
                         context_kind='research_hourly_er20_ema20_v1', anchors=anchor_evidence,
                         features=features, group_scores=group_scores, coverage=coverage, scores=calculated,
                         score_kind='heuristic_not_probability', path_id='research_observe', **SAFETY)
            event['research_event_id'] = digest((sha(HERE/'CONTRACT.md'), event['strategy_id'],
                                                 symbol, event['decision_hash'], side, context_hash))
            events.append(event)
            counts[family] += 1
            counts['context_'+regime] += 1
    return events, dict(counts)


def allowed(e: dict, variant: str, cells: dict[str, dict]) -> bool:
    if variant == 'baseline':
        return True
    if variant in ('regime60', 'session_regime60'):
        if e['regime'] == 'unknown' or (e['regime'] == 'up' and e['side'] < 0) or (e['regime'] == 'down' and e['side'] > 0):
            return False
        if e['scores']['regime60'] < 60:
            return False
        return variant == 'regime60' or cells.get(cell_key(e), {}).get('allowed', False)
    return e['scores'][variant] >= 60


def cell_key(e: dict) -> str:
    return '|'.join(e[k] for k in ('symbol', 'family', 'session', 'regime'))


def fit_cells(measured_train: list[dict]) -> dict[str, dict]:
    collected: dict[str, list[dict]] = {}
    for e in measured_train:
        if e['status'] == 'measured' and pd.Timestamp(e['exit_time']) < SPLIT:
            collected.setdefault(cell_key(e), []).append(e)
    result = {}
    for key, rows in collected.items():
        days = len({e['decision_close'][:10] for e in rows})
        mean = float(np.mean([e['net_bps'] for e in rows]))
        result[key] = dict(n=len(rows), days=days, mean_net_bps=mean,
                           allowed=len(rows) >= 30 and days >= 3 and mean > 0,
                           latest_label_at=max(e['exit_time'] for e in rows))
    return result


def run(root: Path, output: Path) -> None:
    cost = CostModel.for_profile('delta_scalp_v2')
    if cost.config_sha256 != 'e04d112e35708157a77fc5b5721e4e2c4b2c0ce6d05c71b65bff3bf443e7b13a':
        raise ValueError('cost_contract_changed')
    helper = ref('context_measurement', 'gann_slope_20260913')
    minute_helper = ref('context_minutes', 'burst_response_20260912')
    output.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = dict(study='context_score_5m_v1', **SAFETY,
        untouched_oos=False, split_kind='chronological_test_on_previously_seen_data',
        cost_profile_id=cost.profile, cost_config_sha256=cost.config_sha256,
        booked_round_bps=17.8, funding='excluded', fill_assumption='delayed_1m_open_fixed15m_response',
        git_revision=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        source_hashes={}, symbols={}, trained_cells={}, deferred=['elliott', 'harmonics', 'moon', 'renko', 'gann', 'resting_supply_demand'])
    paths = [Path(__file__), HERE/'CONTRACT.md', Path(helper.__file__), Path(minute_helper.__file__),
             HERE.parent/'range_retest_20260912/replay.py', Path('src/vnedge/data/swings.py'),
             Path('src/vnedge/data/bar_identity.py'), Path('src/vnedge/strategy/arm_evidence.py'),
             Path('src/vnedge/plan/cost_model.py')]
    result['source_hashes'] = {str(p.resolve()): sha(p) for p in paths}
    for symbol in ('BTCUSD', 'ETHUSD'):
        bars, bm = helper.load_bars(root, symbol)
        minutes, mm = minute_helper.load_minutes(root, symbol)
        events, counts = generate(bars, symbol)
        with (output/f'{symbol}.features.jsonl').open('x') as stream:
            for e in events:
                stream.write(json.dumps(e, allow_nan=False)+'\n')
        books, attrition = {}, {}
        for family in FAMILIES:
            family_events = [e for e in events if e['family'] == family]
            train = [e for e in family_events if pd.Timestamp(e['decision_close'])+pd.Timedelta(minutes=16) < SPLIT]
            test = [e for e in family_events if pd.Timestamp(e['decision_close']) >= SPLIT+pd.Timedelta(minutes=20)]
            train_regime, _ = helper.measure([e for e in train if allowed(e, 'regime60', {})], minutes)
            cells = fit_cells(train_regime)
            result['trained_cells'].update(cells)
            books[family] = {}
            for variant in VARIANTS:
                # Session-trained rule is evaluated only on test; no in-sample
                # performance fiction from retroactively applying learned cells.
                phases = {'test': test} if variant == 'session_regime60' else {'train': train, 'test': test}
                books[family][variant] = {}
                for phase, source in phases.items():
                    selected = [e for e in source if allowed(e, variant, cells)]
                    measured, flow = helper.measure(selected, minutes)
                    summary = minute_helper.summarize(measured, 17.8)
                    books[family][variant][phase] = dict(summary=summary, counts=flow)
                    attrition[f'{family}|{variant}|{phase}'] = dict(available=len(source), selected=len(selected), **flow)
                    # Every measured/censored outcome carries its feature and decision proof.
                    with (output/f'{symbol}.{family}.{variant}.{phase}.jsonl').open('x') as stream:
                        for e in measured:
                            stream.write(json.dumps(e, allow_nan=False)+'\n')
            # Matched diagnostic attribution never simulates a fresh book: same
            # scheduled baseline events, feature present vs absent, all reported.
            baseline, _ = helper.measure(test, minutes)
            attribution = {}
            for group in GROUPS:
                names = {name for e in baseline for name in e['features'][group]}
                for name in sorted(names):
                    attribution[name] = {label: minute_helper.summarize(
                        [e for e in baseline if e['features'][group][name] is value], 17.8)
                        for label, value in [('present', True), ('absent', False), ('unavailable', None)]}
            books[family]['matched_feature_attribution'] = attribution
        result['symbols'][symbol] = dict(bars=bm, minutes=mm, detector_counts=counts,
                                         books=books, attrition=attrition)
    with (output/'results.json').open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print(json.dumps({s: {f: {v: b[v]['test']['summary'] for v in VARIANTS[:4]}
                              for f, b in d['books'].items()} for s, d in result['symbols'].items()}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=HERE/'attempt_01')
    args = parser.parse_args()
    run(args.input, args.output)
