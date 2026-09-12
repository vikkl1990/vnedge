# HTF scanner and admission-path recheck — 2026-09-12

## Verdict

The frozen scanner produced no trades in this replay. The current targetless
signal cannot pass runtime target admission, even if an edge estimate were
supplied. A separate synthetic positive control passed risk, submitted through
the kernel, and received one simulated fill. This proves a functioning generic
simulation path, not a functioning or profitable HTF strategy.

The proposed corrections have **not** been implemented in a new strategy
revision in this run. These results establish the baseline and isolate the
admission defect; they do not claim the proposed corrected strategy works.

## Historical replay

Frozen IDs: `htf_regime_continuation_15m_v2__BTCUSD` and
`htf_regime_continuation_15m_v2__ETHUSD`, source revision `ab20440`.

Input interval: 2026-09-01 00:00 UTC inclusive through 2026-09-11 00:00 UTC
exclusive. The completed ten-day window is fixed before this run; no parameter
selection was performed. This is already-seen exploratory data, not untouched
OOS. The first 224 rows warm up; the final row is reserved for next-open entry.

| Measurement | BTC | ETH |
|---|---:|---:|
| Expected 15m slots | 960 | 960 |
| Stored canonical decision rows | 946 | 945 |
| Missing slots, not fabricated | 14 | 15 |
| Evaluable rows | 721 | 720 |
| Continuation-denied evaluations | 721 | 720 |
| Directional structure evaluations | 520 | 447 |
| Missing-hour-parent evaluations | 41 | 45 |
| Signals | 0 | 0 |
| Model trades | 0 | 0 |

BTC regime reasons: `weekly_range_macd_off` on all 721 evaluations.
ETH: that reason on 704 evaluations, `daily_4h_macd_impulse_fade` on 16.

Therefore: even with directional structure available, permission prevented
every signal. Fixing structure alone is not sufficient in this interval.

Each report contains SHA-256 hashes of the copied input files, explicit source
counts, context bars and missing timestamps. Decision bars retain canonical
closed/hash/source proof. Official 4h/1d caches are context only, overlaid with
available canonical context using the runtime binding function. No REST data
was silently promoted to canonical decision history.

Cost profiles remain `delta_swing_btc_v1` / `delta_swing_eth_v1`: 15.8 bps
modeled booked cost, 18.8 bps gate estimate. No move to the scalp profile.
Zero trades means net expectancy, PF and profitability are **unmeasured**.

### Important replay limitations

- This scanner-evidence replay is not live admission parity: it does not run
  the target admission, CostGate, sizing and kernel for historical entries.
- Its outcome loop does not reproduce every trailing/strategy exit. Since
  there were zero trades, no performance conclusion relies on that loop.
- Missing candles are absent in the exported canonical frame. The running
  lane can also contain non-armable official fallback rows tagged as gaps.
  Those representations can reset the swing map differently. Counts here
  must not be presented as a reproduction of the live reset history.
- Funding and lane-consumed BBO were not replayed; promotion is prohibited.
- Gap-free days and directional structure do not prove the current weekly/
  daily permission policy is economically useful for a scalper.

## Simulated admission isolation

All four cases generated one **synthetic** signal. The existing isolated proof
fixture used in-memory market data, temporary journals and a simulated adapter;
network connections are blocked. No approving mocks replace cost/risk/kernel.
The positive-control 100-bps expectancy is a fixture constant, not OOS evidence.

| Synthetic signal shape | Submitted | Simulated fills | Result |
|---|---:|---:|---|
| No target, no edge estimate (HTF-like shape) | 0 | 0 | No favorable target/edge hypothesis |
| No target, synthetic edge estimate | 0 | 0 | Same target rejection |
| Target, no edge estimate | 0 | 0 | `edge_estimate_missing` |
| Target and synthetic edge estimate | 1 | 1 | Cost, risk and kernel path accepted |

This tests the signal shape against the runtime, not the HTF setup detector.
Seven existing synthetic execution-proof tests also passed, covering the
accept/fill/exit/journal path and rejection/idempotency cases.

Validation: all four admission assertions passed; full local suite **2,993
passed, 6 skipped**. This suite includes existing uncommitted workspace work;
it is not a clean-release deployment attestation. Both research scripts pass
Ruff. No tests establish profitability or live venue approval parity.

Do **not** add a fake target or invented expectancy to make HTF pass. Reconcile
targetless/trailing-exit contracts with evidence-based admission explicitly,
then validate any fire-set or geometry change under a new revision.

## Reproduce

```sh
PYTHONPATH=src .venv/bin/python research/reports/htf_path_recheck_20260912/run_replay.py \
  --input /tmp/vnedge-htf-recheck.PInWIi \
  --output research/reports/htf_path_recheck_20260912
PYTHONPATH=src .venv/bin/python research/reports/htf_path_recheck_20260912/run_execution_checks.py
.venv/bin/python -m pytest -q tests/test_shadow_execution_proof.py
```

VM, roster, capital permissions, strategy registry and live strategy code were
not modified by this experiment. Research artifacts are local only.
