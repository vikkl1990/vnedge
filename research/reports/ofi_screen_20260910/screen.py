"""Frozen research-only sampled L1 OFI screen; never creates an order."""
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import json

import numpy as np
import pandas as pd

from vnedge.plan.cost_model import CostModel
from vnedge.research.scanner_evidence import atomic_write, load_quote_evidence_window

ROOT = Path('/tmp/vnedge-range-baseline.U7rGgi/data/quote_evidence')
OUT = Path('research/reports/ofi_screen_20260910')
START = datetime(2026, 9, 4, tzinfo=UTC)
END = datetime(2026, 9, 5, tzinfo=UTC)
CUTOFF = int(pd.Timestamp('2026-09-04T12:00:00Z').timestamp() * 1000)
END_MS = int(END.timestamp() * 1000)


def increments(bid, ask, bid_size, ask_size):
    return (
        bid.ge(bid.shift()) * bid_size - bid.le(bid.shift()) * bid_size.shift()
        - ask.le(ask.shift()) * ask_size + ask.ge(ask.shift()) * ask_size.shift()
    )


# Pin signs: more bid at same price, more ask at same price, higher bid.
assert increments(pd.Series([100., 100.]), pd.Series([101., 101.]),
                  pd.Series([10., 15.]), pd.Series([10., 10.])).iloc[1] == 5
assert increments(pd.Series([100., 100.]), pd.Series([101., 101.]),
                  pd.Series([10., 10.]), pd.Series([10., 15.])).iloc[1] == -5
assert increments(pd.Series([100., 100.5]), pd.Series([101., 101.]),
                  pd.Series([10., 15.]), pd.Series([10., 10.])).iloc[1] == 15

model = CostModel.for_profile('delta_scalp')
booked = model.round_trip_bps(include_safety=False)
fees = 2 * model.config.taker_fee_bps * model.config.fee_gst_mult
all_results = []
for symbol in ('BTCUSD', 'ETHUSD'):
    print('Loading', symbol, flush=True)
    qroot = next(ROOT.glob(f'*/exchange=delta_india/symbol={symbol}/20260904'))
    q = load_quote_evidence_window(qroot, start=START, end=END)
    q = q.sort_values(['received_ts_ms', 'captured_at_ms'], kind='stable').reset_index(drop=True)
    seq = pd.to_numeric(q.sequence, errors='coerce')
    prior_max = seq.cummax().shift()
    advanced = seq.gt(prior_max) | prior_max.isna()
    duplicate = seq.eq(prior_max)
    age = q.received_ts_ms - q.ts_ms
    valid = (advanced & seq.notna() & q.bid.lt(q.ask)
             & q.bid.gt(0) & q.ask.gt(0) & q.bid_size.gt(0) & q.ask_size.gt(0)
             & age.between(0, 1000) & q.overflow_drops.eq(0)
             & q.capture_overflow_drops.eq(0) & q.exchange_timestamped.eq(True))
    bad_minutes = set((q.loc[~valid & ~duplicate, 'received_ts_ms'] // 60000).astype(int))
    stats = {'input_rows': len(q), 'duplicate_sequences': int(duplicate.sum()),
             'nonduplicate_invalid_rows': int((~valid & ~duplicate).sum()),
             'valid_updates': int(valid.sum())}
    f = q.loc[valid, ['received_ts_ms', 'bid', 'ask', 'bid_size', 'ask_size']].copy().reset_index(drop=True)
    del q
    f['minute'] = (f.received_ts_ms // 60000).astype('int64')
    f['gap'] = f.received_ts_ms.diff()
    f['e'] = increments(f.bid, f.ask, f.bid_size, f.ask_size)
    f['depth'] = f.bid_size + f.ask_size
    g = f.groupby('minute', sort=True).agg(
        n=('e', 'count'), first=('received_ts_ms', 'min'), last=('received_ts_ms', 'max'),
        max_gap=('gap', 'max'), ofi=('e', 'sum'), depth=('depth', 'mean'),
    )
    minute_close = (g.index + 1) * 60000
    good = (g.n.ge(10) & (g['first'] - g.index * 60000).le(5000)
            & (minute_close - g['last']).le(5000) & g.max_gap.le(5000)
            & ~g.index.isin(bad_minutes) & g.depth.gt(0))
    g['x'] = g.ofi / g.depth
    train = g.loc[good & (minute_close <= CUTOFF)]
    threshold = float(train.x.abs().quantile(.90))
    # Exclude the final calibration minute's own decision from validation.
    decisions = g.loc[good & (minute_close > CUTOFF) & g.x.abs().gt(threshold)]
    stats.update({'valid_minutes': int(good.sum()), 'calibration_minutes': len(train),
                  'threshold_abs_ofi': threshold, 'validation_candidate_minutes': len(decisions)})
    t = f.received_ts_ms.to_numpy(dtype='int64')
    bid = f.bid.to_numpy(float); ask = f.ask.to_numpy(float)
    mid = (bid + ask) / 2
    gap_prefix = np.cumsum(np.r_[False, np.diff(t) > 5000])
    for horizon in (60, 300):
        records = []; rejected = Counter(); occupied_until = 0
        for minute, row in decisions.iterrows():
            decision = (int(minute) + 1) * 60000
            if decision <= occupied_until:
                rejected['position_overlap'] += 1; continue
            target = decision + 250
            i = int(np.searchsorted(t, target))
            if i >= len(t) or t[i] - target > 1000:
                rejected['entry_quote_missing'] += 1; continue
            exit_target = t[i] + horizon * 1000
            j = int(np.searchsorted(t, exit_target))
            if j >= len(t) or t[j] >= END_MS or t[j] - exit_target > 1000:
                rejected['exit_quote_missing'] += 1; continue
            if gap_prefix[j] != gap_prefix[i]:
                rejected['holding_quote_gap'] += 1; continue
            side = 1 if row.x > 0 else -1
            entry = ask[i] if side == 1 else bid[i]
            exit_price = bid[j] if side == 1 else ask[j]
            gross = side * (exit_price - entry) / entry * 10000
            records.append({'decision_close_ms': decision, 'entry_ms': int(t[i]),
                            'exit_ms': int(t[j]), 'side': 'long' if side == 1 else 'short',
                            'ofi': float(row.x), 'entry_price': float(entry), 'exit_price': float(exit_price),
                            'mid_markout_bps': float(side * (mid[j] - mid[i]) / mid[i] * 10000),
                            'spread_crossed_gross_bps': float(gross), 'net_bps': float(gross - booked),
                            'fees_only_net_bps': float(gross - fees)})
            occupied_until = int(t[j])
        net = np.array([r['net_bps'] for r in records])
        gross = np.array([r['spread_crossed_gross_bps'] for r in records])
        loss = float(-net[net < 0].sum())
        result = {'symbol': symbol, 'horizon_seconds': horizon, 'measurements': len(records),
                  'mean_gross_bps': float(gross.mean()) if len(net) else None,
                  'mean_net_bps': float(net.mean()) if len(net) else None,
                  'median_net_bps': float(np.median(net)) if len(net) else None,
                  'mean_fees_only_net_bps': float((gross - fees).mean()) if len(net) else None,
                  'win_rate_pct': float((net > 0).mean() * 100) if len(net) else None,
                  'profit_factor': float(net[net > 0].sum() / loss) if loss else None,
                  'rejections': dict(rejected), 'input_stats': stats, 'records': records,
                  'can_trade': False, 'can_promote': False, 'performance_eligible': False}
        all_results.append(result)
        print(json.dumps({k: v for k, v in result.items() if k != 'records'}), flush=True)
    del f

atomic_write(OUT / 'results.json', {
    'generated_at': datetime.now(UTC).isoformat(),
    'experiment_id': 'sampled_l1_ofi_measurement_v1', 'type': 'exploratory_temporal_screen',
    'contract_sha256': sha256((OUT / 'CONTRACT.md').read_bytes()).hexdigest(),
    'script_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
    'input_manifest': '../range_baseline_20260910/input_manifest.json',
    'cost_profile': model.profile, 'booked_fee_and_slippage_bps': booked,
    'fee_gst_only_sensitivity_bps': fees, 'observed_spread': 'already_crossed_in_gross',
    'funding': 'not_modeled', 'size_and_depth_fill_validation': False,
    'can_trade': False, 'can_promote': False, 'performance_eligible': False,
    'results': all_results,
})
