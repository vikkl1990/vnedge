# VWAP consolidation breakout: first frozen test

**No edge established.** The baseline and volume-filtered variants lost after
modeled execution costs. The VWAP filter produced only one measured trade per
symbol; the combined filter produced zero. Those samples cannot determine
whether VWAP adds value, let alone identify institutional activity.

## Matched results

Delta India BTCUSD and ETHUSD, September 1–10, 2026. Already-seen exploratory
data, not untouched OOS. Long-only, 5m close → next 5m open; canonical 1m
execution proxy, stop-first ties, fixed 30-minute maximum holding time.

| Variant | BTC trades | BTC gross bps/trade | BTC after modeled costs* | ETH trades | ETH gross bps/trade | ETH after modeled costs* |
|---|---:|---:|---:|---:|---:|---:|
| Consolidation breakout | 29 | -0.22 | -18.02 | 42 | -8.60 | -26.39 |
| + exact session VWAP | 1 | +8.27 | -9.54 | 1 | -27.10 | -44.86 |
| + volume confirmation | 8 | +0.60 | -17.19 | 14 | -15.77 | -33.55 |
| + VWAP and volume | 0 | — | — | 0 | — | — |

\*Before funding. Funding is unavailable, not zero: final `net_bps` remains
null in every artifact. Frozen `delta_scalp_v2`: 5.9bps fee including GST per
fill, 3bps adverse execution per leg embedded in prices. Fees use actual
simulated entry/exit notionals; no second slippage subtraction. Nominal
roundtrip cost is 17.8bps. The 19.8bps profile wall is recorded only, never
deducted as PnL; no live CostGate or scanner configuration changed.

## What the tape says

- The baseline's gross movement was already weak/negative. Costs did not
  hide a strong positive underlying response in this test.
- 50 of 71 baseline episodes exited by timeout, 18 by stop and 3 by target.
  That is limited follow-through under these particular frozen rules.
- The volume filter did not rescue either symbol. Its selected-minus-rejected
  daily-bootstrap interval crosses zero for both; no demonstrated uplift.
- The one BTC VWAP trade was positive gross but negative after modeled costs.
  A single trade cannot establish a filtering advantage. Its degenerate
  bootstrap interval in the machine artifact is NOT meaningful uncertainty.
- Doubling adverse execution to 6bps per leg on the same events worsens
  baseline expectancy to -24.01bps BTC and -32.37bps ETH before funding.

## Uncertainty, coverage and drawdown

Baseline descriptive day-bootstrap 95% mean intervals:
BTC [-24.83, -12.44]bps; ETH [-35.13, -16.88]bps, before funding.
Only ten calendar days are available. These are descriptive intervals, not
multiple-testing-adjusted significance or proof of future behavior.

Baseline cumulative unit-notional drawdowns are 522.53bps BTC and 1,108.29bps
ETH. These are sums of unweighted trade returns, **not account percentage
drawdowns**; the replay does not implement sizing, portfolio risk or leverage.

Of 2,880 five-minute slots per symbol, 2,864 BTC and 2,863 ETH passed stored
closed-bar/identity plus exact five-child checks. Sixteen/seventeen rows lacked
closed proof. Enforcing uninterrupted coverage since midnight leaves only
2,397 BTC / 2,393 ETH eligible session slots. Invalid slots poison the rest of
that UTC session; no gap or missing volume was filled. All 71 emitted baseline
episodes had observable execution paths; no emitted episodes were censored.

Chronological reporting blocks (not OOS): baseline BTC first seven days
24 trades / -18.39bps, last three days 5 / -16.22bps; ETH 36 / -28.52bps and
6 / -13.57bps. Session and local-regime tables retain every predefined bucket
in results.json. No favorable bucket was selected for activation.

## Limits and decision

This tests one explicit numerical interpretation, not every possible VWAP
setup. The combined hypothesis remains under-sampled, not disproved. Do not
loosen it after reading these outcomes. If pursued, freeze the same rules for
a longer coverage-proven recording window and establish settled funding and
execution realism before any promotion judgment.

No strategy registered, no runtime or roster changes, no deployment, no orders.
The current bot remains unchanged. Research evidence is not ML training labels.

## Reproduce and audit

- [Frozen contract](CONTRACT.md)
- [Replay harness](replay.py)
- [Complete results](attempt_01/results.json)
- [Independent verification](attempt_01/verification.json)
- `tests/test_vwap_consolidation_research.py`: six causality, exact-VWAP,
  gap, tie, missing-path and cash-cost regression tests.

Run against the frozen export with a **new** output directory; existing attempt
files cannot be overwritten. Verification checked all input hashes, 71 measured
baseline episodes, 95 distinct variant decision IDs, cost arithmetic, counts
and unit-notional drawdowns. The 95 IDs are overlapping ablation memberships,
not 95 independent market opportunities.
