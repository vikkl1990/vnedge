# Range-break/retest: first frozen execution replay

**No positive edge found.** The new logic emitted 11 candidates on the ten-day
Delta window; all 11 had measurable simulated outcomes. BTC and ETH both had
negative average gross movement before fees. Each symbol is under-sampled, so
this is an unfavorable exploratory screen, not a statistically established
failure across all market conditions. Do not promote or deploy this ID.

Strategy: `range_break_retest_5m_v1`, unchanged from its built specification.
Window: September 1–10, 2026 UTC, already seen in other research, **not OOS**.
See the [frozen replay contract](CONTRACT.md).

## Outcome

| Metric | BTCUSD | ETHUSD |
|---|---:|---:|
| Stored 5m decisions evaluated | 2,864 | 2,863 |
| Missing scheduled decision rows | 16 | 17 |
| Candidate signals | 5 | 6 |
| Measured simulated outcomes | **5** | **6** |
| Entry-gap rejections / overlap / censoring | 0 / 0 / 0 | 0 / 0 / 0 |
| Stops | 4 | 2 |
| Targets | **0** | **0** |
| 15-minute timeouts | 1 | 4 |
| Average gross movement | −22.40 bps | −8.33 bps |
| Average net at modeled 17.8 bps cost | **−40.20 bps** | **−26.13 bps** |
| Net positive outcomes | 0 / 5 | 1 / 6 |
| Net profit factor | 0.00 | 0.05 |
| Fee-only 11.8 bps sensitivity | −34.20 bps | −20.13 bps |
| Stress 23.8 bps sensitivity | −46.20 bps | −32.13 bps |
| First five days average net | −41.69 bps (n=4) | −32.20 bps (n=2) |
| Second five days average net | −34.24 bps (n=1) | −23.09 bps (n=4) |
| Classification | INSUFFICIENT_SAMPLE | INSUFFICIENT_SAMPLE |

The two five-day slices are reporting blocks, not independent OOS tests. These
are unleveraged price-normalized simulated trade returns, not account returns.
Changing from basis points to dollars cannot turn their negative gross
averages positive. A lower modeled bill alone would not rescue this sample.

### Every simulated outcome

Times are decision close / next-open entry, UTC. Original stop and target
levels, exact evidence IDs and prices are retained in the JSON artifact.

| Symbol | Entry UTC (2026) | Side | Exit | Gross bps | Net bps |
|---|---|---|---|---:|---:|
| BTCUSD | 09-02 09:45 | short | stop | −18.62 | −36.42 |
| BTCUSD | 09-03 13:45 | long | timeout | −6.09 | −23.89 |
| BTCUSD | 09-03 15:05 | long | stop | −52.94 | −70.74 |
| BTCUSD | 09-03 20:00 | long | stop | −17.91 | −35.71 |
| BTCUSD | 09-08 16:00 | long | stop | −16.44 | −34.24 |
| ETHUSD | 09-03 14:30 | long | timeout | +25.92 | +8.12 |
| ETHUSD | 09-03 16:00 | long | stop | −54.73 | −72.53 |
| ETHUSD | 09-06 23:40 | long | timeout | −21.83 | −39.63 |
| ETHUSD | 09-07 01:50 | short | stop | −10.94 | −28.74 |
| ETHUSD | 09-07 17:50 | long | timeout | +2.21 | −15.59 |
| ETHUSD | 09-08 15:45 | long | timeout | +9.42 | −8.38 |

## Interpretation

The software does find and bind its named setup. But a breakout/retest shape
plus apparently sufficient target room did not predict a profitable short-hold
response here. Six setups invalidated at their structural stops and the other
five ran out of time; none reached the projected measured-range target.

That is the distinction between **target geometry and expected edge**. A
price level far enough away to cover fees is not evidence that price will reach
it. This test does not establish that extending the hold, reducing stops, or
changing the retest tolerance would improve results. Those would be new,
separately preregistered experiments, not fixes applied to this seen window.

The quality score is not calibrated. For example, the ETH candidate scoring
80.6 lost 72.53 bps net; the only positive ETH result scored 60.0. These examples
illustrate why scores cannot be called win probabilities, not a statistical
case for reversing or retuning the scorer.

## Why most bars did not produce a candidate

| Primary failed gate | BTC | ETH |
|---|---:|---:|
| No fresh range break | 2,345 | 2,373 |
| Gap/duplicate within required 22-bar window | 198 | 219 |
| Breakout body too small | 117 | 103 |
| Boundary retest missing | 100 | 67 |
| Breakout overextended | 40 | 43 |
| Initial warmup | 21 | 21 |
| Retest hold failed | 11 | 14 |
| Retest entry overextended | 9 | 4 |
| Net reward/risk too small | 8 | 8 |
| Retest close location weak | 6 | 5 |
| Target room below cost floor | 4 | 0 |

The artifact also contains **all** failed gates per evaluated setup; primary
counts are not interchangeable with that overlapping histogram. Gaps remain
rejections, not synthetic candles. This ID does not depend on daily EMA200,
HTF MACD, an ML model or BBO acceptance, so those are not the explanation for
this screen's low count or negative returns.

## What was actually tested

- Original stored, hashed canonical 5m decision candles; no repaired or official
  fallback. Canonical 1m history is independently hash/quality checked for the
  execution path. No rewriting the strategy source after inspecting outcomes.
- Entry at the next 5m open. Rechecked broken-level hold, stop/target ordering,
  chase cap, cost room and net reward/risk at that price. No stop or target moved.
- Minute-by-minute stop/target resolution, stop-first same-minute ties, worse
  adverse-gap opens, no favorable target-gap improvement; otherwise fifteenth
  minute close. The declared 15-minute hold was not extended after seeing losses.
- Net movement subtracts the frozen full-taker `delta_scalp_v2` cost once:
  11.8 fee/GST +6 modeled execution friction =17.8 bps. No assumed maker rebate,
  no waiver, no second slippage deduction. Funding remains excluded.

**Limits:** the next-open convention assumes zero reaction delay, and minute
OHLC cannot show order-book depth, tradable bid/ask, exact intraminute fill time,
queue priority, or actual venue execution. No account sizing, reconciliation,
kernel approval, or ManagedOrder was run. This is a candle execution simulation,
not live/paper/shadow approval parity. Strategy and symbol scoreboards stay separate
from the older external-stack fixed-horizon test.

## Evidence and reproduction

- [Replay harness](replay.py), [exit tests](test_replay.py), [verifier](verify.py).
- [Full result](attempt_01/results.json), [verification](attempt_01/verification.json).
- `attempt_01/BTCUSD.evaluations.jsonl` and `ETHUSD.evaluations.jsonl`: every
  evaluation, diagnostics, score components and emitted evidence wrapper.
- Source/spec/input hashes and cost-config identity are retained in the result.
  The verifier checked all inputs and eight source files unchanged, rebuilt
  envelope validation, reconciled candidate counts and recomputed cost arithmetic.
- Software verification: 42 focused strategy/exit-path tests passed; full
  repository suite 3,026 passed, 6 skipped (611 dependency warnings). Ruff passes.
  Passing software tests is not passing the economic screen.
- Static inputs remain `/tmp/vnedge-htf-recheck.PInWIi`; preserve matching files
  for reproduction. Hash manifests are not a substitute for those data files.

```sh
.venv/bin/python research/reports/range_retest_20260912/replay.py --output /absolute/path/to/new/attempt
.venv/bin/python research/reports/range_retest_20260912/verify.py --attempt /absolute/path/to/new/attempt
```

Reproduction is not a new independent experiment. The harness refuses to
overwrite an existing attempt. No registry/roster, live permission, scanner
parameters, or VM service changed. All artifacts remain `can_trade=false`,
`can_promote=false`, `performance_eligible=false`.
