"""Read-only verification of frozen outcomes; writes a separate audit, no refit."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('context_verify', HERE/'screen.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run() -> None:
    attempt = HERE/'attempt_01'
    r = json.loads((attempt/'results.json').read_text())
    for path, expected in r['source_hashes'].items():
        assert m.sha(Path(path)) == expected, ('source_changed', path)
    for d in r['symbols'].values():
        for source in d['bars']['files'] + d['minutes']['files']:
            assert m.sha(Path(source['path'])) == source['sha256']
    helper = m.ref('context_verify_minutes', 'burst_response_20260912')
    checks, verified_rows, corrected_intervals = 0, 0, {}
    for symbol, d in r['symbols'].items():
        minutes, _ = helper.load_minutes(Path('/tmp/vnedge-htf-recheck.PInWIi'), symbol)
        for family in m.FAMILIES:
            trained = []
            for variant in m.VARIANTS:
                for phase, book in d['books'][family][variant].items():
                    path = attempt/f'{symbol}.{family}.{variant}.{phase}.jsonl'
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    assert len({e['research_event_id'] for e in rows}) == len(rows)
                    previous_exit = m.START
                    measured = []
                    for e in rows:
                        close, entry, exit_at = map(pd.Timestamp, (e['decision_close'], e['entry_time'], e['exit_time']))
                        assert close >= previous_exit
                        previous_exit = exit_at  # even censored records reserve the interval
                        assert entry == close+pd.Timedelta(minutes=1)
                        assert exit_at == entry+pd.Timedelta(minutes=15)
                        assert (exit_at < m.SPLIT if phase == 'train' else close >= m.SPLIT+pd.Timedelta(minutes=20))
                        assert m.allowed(e, variant, r['trained_cells'])
                        assert all(e[k] is False for k in m.SAFETY)
                        prices = minutes.reindex(pd.date_range(close, exit_at, freq='min'))
                        valid = bool(prices.eligible.eq(True).all())
                        assert valid == (e['status'] == 'measured')
                        if valid:
                            ep, xp = float(prices.loc[entry, 'open']), float(prices.loc[exit_at, 'open'])
                            expected_gross = e['side']*(xp/ep-1)*10_000
                            assert abs(expected_gross-e['gross_bps']) < 1e-9
                            assert abs(e['net_bps']-(expected_gross-17.8)) < 1e-9
                            measured.append(e)
                        verified_rows += 1
                    summary = book['summary']
                    assert summary['n'] == len(measured)
                    if measured:
                        gross = np.array([e['gross_bps'] for e in measured])
                        net = gross-17.8
                        assert abs(summary['net_mean_bps']-net.mean()) < 1e-9
                        assert abs(summary['fee_only_mean_bps']-(gross-11.8).mean()) < 1e-9
                        assert abs(summary['stress_mean_bps']-(gross-23.8).mean()) < 1e-9
                        loss = -net[net < 0].sum()
                        if loss:
                            assert abs(summary['profit_factor']-net[net > 0].sum()/loss) < 1e-9
                        equity = np.r_[0., np.cumsum(net)]
                        dd = (np.maximum.accumulate(equity)-equity).max()
                        assert abs(summary['drawdown_cumulative_net_bps']-dd) < 1e-9
                    if variant == 'regime60' and phase == 'train':
                        trained.extend(rows)
                    # The inherited helper resamples all10 days even for a5-day
                    # phase. Keep original artifacts intact; do NOT use that CI.
                    # This audit supplies a phase-specific descriptive interval.
                    days = pd.date_range(m.START if phase == 'train' else m.SPLIT,
                                         m.SPLIT if phase == 'train' else m.END, freq='D', inclusive='left')
                    by_day = [[e['net_bps'] for e in measured if pd.Timestamp(e['decision_close']).floor('D') == day]
                              for day in days]
                    totals, counts = np.array([sum(v) for v in by_day]), np.array([len(v) for v in by_day])
                    interval = None
                    if len(measured) >= 30 and np.count_nonzero(counts) >= 3:
                        indices = np.random.default_rng(20260913).integers(0, len(days), (2000, len(days)))
                        ns = counts[indices].sum(axis=1)
                        sums = totals[indices].sum(axis=1)
                        interval = np.quantile(sums[ns > 0]/ns[ns > 0], [.025, .975]).tolist()
                    corrected_intervals[f'{symbol}|{family}|{variant}|{phase}'] = dict(
                        n=len(measured), calendar_days=len(days), active_days=int(np.count_nonzero(counts)),
                        descriptive_day_bootstrap_95=interval, inferential_significance=False)
                    checks += 1
            assert m.fit_cells(trained) == {k: v for k, v in r['trained_cells'].items()
                                           if k.startswith(symbol+'|'+family+'|')}
    audit = dict(verified=True, books=checks, event_records=verified_rows,
                 raw_result_sha256=m.sha(attempt/'results.json'), verifier_sha256=m.sha(Path(__file__)),
                 original_helper_confidence_intervals='DO_NOT_USE_wrong_resampling_calendar_for_split',
                 phase_specific_descriptive_intervals=corrected_intervals, **m.SAFETY)
    with (attempt/'verification.json').open('x') as stream:
        json.dump(audit, stream, indent=2, allow_nan=False)
    print(json.dumps({k: v for k, v in audit.items() if k != 'phase_specific_descriptive_intervals'}, indent=2))


if __name__ == '__main__':
    run()
