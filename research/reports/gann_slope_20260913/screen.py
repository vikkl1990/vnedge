"""One frozen slope-cross price-response test; no execution or promotion authority."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import subprocess
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from vnedge.data.candles import Candle
from vnedge.data.swings import detect_swings
from vnedge.plan.cost_model import CostModel
from vnedge.strategy.arm_evidence import assert_decision_row

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
START = pd.Timestamp('2026-09-01T00:00:00Z')
END = pd.Timestamp('2026-09-11T00:00:00Z')
STEP = pd.Timedelta(minutes=5)
SPEC = dict(strategy_id='gann_normalized_slope_cross_5m_v1', left=3, right=3,
            strict=True, atr_period=20, slope_divisor=20, max_confirmation_age=20,
            volume_multiple=1.5, delay_minutes=1, hold_minutes=15,
            cost_profile_id='delta_scalp_v2', booked_round_bps=17.8)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def reference(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HERE.parent / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_bars(root: Path, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    loader = reference('gann_decision_loader', 'range_retest_20260912/replay.py')
    frame, manifest = loader.load_decisions(root, symbol)
    reasons: Counter = Counter()
    usable = []
    for row in frame.to_dict('records'):
        failed = []
        try:
            assert_decision_row(row, timeframe='5m')
        except ValueError as exc:
            failed.append(str(exc))
        if row.get('coverage_ok') is not True:
            failed.append('coverage_missing')
        for key in ('volume', 'quote_volume'):
            value = float(row[key])
            if not math.isfinite(value) or value <= 0:
                failed.append('invalid_' + key)
        reasons.update(failed)
        usable.append(not failed)
    frame['eligible'] = usable
    stored = len(frame)
    frame = frame.set_index('timestamp').reindex(
        pd.date_range(START, END, freq='5min', inclusive='left'))
    frame['eligible'] = frame.eligible.eq(True)
    for col in ('open', 'high', 'low', 'close', 'quote_volume'):
        frame[col] = frame[col].astype(float)
    return frame, dict(files=manifest, stored=stored, expected=len(frame),
                       eligible=int(frame.eligible.sum()), invalid=dict(reasons))


def candle(symbol: str, ts: pd.Timestamp, row: pd.Series) -> Candle:
    return Candle(symbol=symbol, timeframe='5m', open_time=ts.to_pydatetime(),
                  close_time=(ts + STEP).to_pydatetime(),
                  **{k: Decimal(str(row[k])) for k in ('open', 'high', 'low', 'close')},
                  volume=Decimal(str(row['volume'])),
                  quote_volume=Decimal(str(row['quote_volume'])), trade_count=int(row['trade_count']),
                  is_closed=bool(row['is_closed']))


def generate(bars: pd.DataFrame, symbol: str) -> tuple[list[dict], list[dict], dict]:
    """Only prefix slices are used; invalid calendar slots reset persistent anchors."""
    main, baseline = [], []
    counts: Counter = Counter()
    anchors: dict[str, dict] = {}
    used: set[str] = set()
    tr = pd.concat([bars.high - bars.low,
                    (bars.high - bars.close.shift()).abs(),
                    (bars.low - bars.close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(20).mean()
    prior_volume = bars.quote_volume.shift().rolling(20).median()
    for i, (ts, row) in enumerate(bars.iterrows()):
        if not row.eligible:
            anchors.clear()
            counts['invalid_or_missing_decision'] += 1
            continue
        if i < 20 or not bars.eligible.iloc[i-20:i+1].all():
            counts['warmup_or_context_gap'] += 1
            continue
        window = bars.iloc[i-6:i+1]
        swings = detect_swings([candle(symbol, t, r) for t, r in window.iterrows()])
        for pivot in swings:
            kind = pivot.kind.value
            pi = i - 3
            anchor = dict(kind=kind, pivot_index=pi, confirm_index=i,
                          anchor_open=bars.index[pi].isoformat(),
                          confirmed_at=(ts + STEP).isoformat(),
                          anchor_hash=str(bars.iloc[pi].content_sha256),
                          confirmation_hash=str(row.content_sha256),
                          price=float(pivot.anchor_price), atr=float(atr.iloc[i]),
                          slope=float(atr.iloc[i]) / 20)
            anchor['episode_id'] = identity((SPEC, symbol, kind, anchor['anchor_hash'],
                                             anchor['confirmation_hash']))
            anchors[kind] = anchor
            counts['anchors_confirmed'] += 1
        if row.close == row.open:
            counts['flat_body'] += 1
            continue
        if row.quote_volume < 1.5 * prior_volume.iloc[i]:
            counts['volume_not_confirmed'] += 1
            continue
        side = 1 if row.close > row.open else -1
        event = dict(decision_open=ts.isoformat(), decision_close=(ts+STEP).isoformat(),
                     side=side, decision_hash=str(row.content_sha256),
                     volume_ratio=float(row.quote_volume/prior_volume.iloc[i]))
        baseline.append(event)
        anchor = anchors.get('swing_high' if side == 1 else 'swing_low')
        if anchor is None or not 1 <= i-anchor['confirm_index'] <= 20:
            counts['anchor_unavailable_or_expired'] += 1
            continue
        # Downward resistance for longs, upward support for shorts.
        prev_line = anchor['price'] - side * anchor['slope'] * (i-anchor['pivot_index'])
        line = prev_line - side * anchor['slope']
        if not (side*(bars.close.iloc[i-1]-prev_line) <= 0 < side*(row.close-line)):
            counts['no_fresh_line_cross'] += 1
            continue
        if anchor['episode_id'] in used:
            counts['episode_already_emitted'] += 1
            continue
        used.add(anchor['episode_id'])
        main.append(dict(event, anchor=anchor.copy(), previous_line=prev_line, line=line))
    counts['slope_events'] = len(main)
    counts['volume_only_events'] = len(baseline)
    return main, baseline, dict(counts)


def measure(events: list[dict], minutes: pd.DataFrame) -> tuple[list[dict], dict]:
    records = []
    counts: Counter = Counter(raw_events=len(events))
    occupied = START
    for event in events:
        close = pd.Timestamp(event['decision_close'])
        if close < occupied:
            counts['overlap_skipped'] += 1
            continue
        entry = close + pd.Timedelta(minutes=1)
        exit_at = entry + pd.Timedelta(minutes=15)
        occupied = exit_at
        path = minutes.reindex(pd.date_range(close, exit_at, freq='min'))
        record = dict(event, entry_time=entry.isoformat(), exit_time=exit_at.isoformat())
        if not path.eligible.eq(True).all():
            record['status'] = 'censored_missing_or_invalid_future'
            counts['censored'] += 1
        else:
            price, exit_price = float(path.loc[entry, 'open']), float(path.loc[exit_at, 'open'])
            gross = event['side'] * (exit_price/price-1) * 10000
            record.update(status='measured', entry_proxy=price, exit_proxy=exit_price,
                          gross_bps=gross, net_bps=gross-17.8)
            counts['measured'] += 1
        records.append(record)
    return records, dict(counts)


def run(root: Path, output: Path) -> None:
    cost = CostModel.for_profile('delta_scalp_v2')
    assert cost.config_sha256 == 'e04d112e35708157a77fc5b5721e4e2c4b2c0ce6d05c71b65bff3bf443e7b13a'
    assert abs(cost.round_trip_bps(include_safety=False)-17.8) < 1e-9
    ref = reference('gann_minute_loader', 'burst_response_20260912/screen.py')
    paths = [Path(__file__), HERE/'CONTRACT.md', Path(ref.__file__),
             HERE.parent/'range_retest_20260912/replay.py',
             ROOT/'src/vnedge/data/swings.py', ROOT/'src/vnedge/data/bar_identity.py',
             ROOT/'src/vnedge/strategy/arm_evidence.py', ROOT/'src/vnedge/plan/cost_model.py']
    output.mkdir(parents=True, exist_ok=False)
    result = dict(spec=SPEC, contract_written_before_run=True,
                  source_hashes={str(p.resolve()): sha(p) for p in paths},
                  git_revision=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                  start=START.isoformat(), end_exclusive=END.isoformat(),
                  cost_config_sha256=cost.config_sha256, funding='excluded',
                  untouched_oos=False, can_trade=False, can_promote=False,
                  performance_eligible=False, symbols={})
    for symbol in ('BTCUSD', 'ETHUSD'):
        bars, bm = load_bars(root, symbol)
        minutes, mm = ref.load_minutes(root, symbol)
        main, baseline, gates = generate(bars, symbol)
        cells = {}
        for name, candidates in [('slope_cross', main), ('volume_only_ablation', baseline)]:
            events, counts = measure(candidates, minutes)
            cells[name] = dict(counts=counts, summary=ref.summarize(events, 17.8), events=events)
        result['symbols'][symbol] = dict(decision_inputs=bm, minute_inputs=mm,
                                         gate_counts=gates, cells=cells)
    with (output/'results.json').open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print(json.dumps({s: {k: v['summary'] for k, v in d['cells'].items()}
                      for s, d in result['symbols'].items()}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=HERE/'attempt_01')
    args = parser.parse_args()
    run(args.input, args.output)
