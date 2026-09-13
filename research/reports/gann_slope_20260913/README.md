# Gann-inspired slope-cross: one frozen historical screen

2026-09-13 · research only · **unsupported at modeled costs**

The screenshot was not a complete strategy specification. We tested one explicit
normalised slope-cross interpretation, NOT Gann's historical system, square of
nine, or the poster's unsupported `P × T = constant` equation. These results do
not establish that every Gann variant works or fails.

## Fixed interpretation

Original canonical Delta 5m bars, BTCUSD/ETHUSD, September 1–10, 2026 UTC.
Strict 3/3 price swings; slope frozen to arithmetic ATR20 / 20 at confirmation.
A high projects descending resistance for a bullish crossing; a low projects
ascending support for a bearish crossing. The line is only usable after swing
confirmation, expires after 20 bars and resets on a data gap. Quote notional
must be at least 1.5× the previous 20-bar median. One event per anchor/side.

Entry proxy is the 1m open one minute after the decision close; exit proxy is
15 minutes later. No stop, target, partial exit or trailing rule was specified
in the poster, so none was fitted to these results. This is fixed-horizon
price-response backtesting, **not a complete execution strategy**.

All choices were written in [CONTRACT.md](CONTRACT.md) before the single run.
No historical rerun, search or parameter change followed the result.

## Main result

All returns are basis points of entry notional, not leveraged account returns.

| Measurement | BTCUSD | ETHUSD |
|---|---:|---:|
| Raw slope-cross events | 105 | 102 |
| Overlapping events excluded | 6 | 5 |
| Missing/invalid future paths censored | 1 | 1 |
| Measured entries | **98** | **96** |
| Average gross movement | +1.09 | +6.58 |
| Average net, 17.8 bps modeled cost | **−16.71** | **−11.22** |
| Median net | −19.63 | −15.66 |
| Net-positive fraction | 12.2% | 18.8% |
| Net profit factor | 0.14 | 0.37 |
| Fee-only sensitivity, 11.8 bps | −10.71 | −5.22 |
| Stress sensitivity, 23.8 bps | −22.71 | −17.22 |
| Net mean after removing best outcome | −18.36 | −13.88 |
| First five days net mean | −13.93 (n=50) | −7.09 (n=52) |
| Second five days net mean | −19.60 (n=48) | −16.10 (n=44) |

The gross directional movement is positive, unlike some earlier patterns, but
is too small to cover this frozen bill. Even removing all 6 bps of modeled
execution friction leaves both symbols negative after the fee/GST component.
The five-day blocks are descriptive reporting slices, not independent OOS.

## Does the slope add anything?

One descriptive comparison was preregistered: keep volume, candle direction,
21-good-bar prerequisite and timing, but remove the anchor/slope restriction.

| Arm | BTC n | BTC gross/net mean | ETH n | ETH gross/net mean |
|---|---:|---:|---:|---:|
| Slope + volume | 98 | +1.09 / −16.71 | 96 | +6.58 / −11.22 |
| Volume + direction only | 316 | +1.09 / −16.71 | 307 | +2.27 / −15.53 |

BTC's gross mean is effectively unchanged. ETH's slope-filtered sample has a
higher gross mean by about 4.31 bps, but remains negative net. The groups are
not matched or randomised and their independent non-overlap filters select
different time exposures. This is NOT proof of causal improvement or statistical
significance. Do not add their counts as independent evidence or promote ETH.

## Data and exclusions

| Input | BTCUSD | ETHUSD |
|---|---:|---:|
| Stored eligible 5m decision bars | 2,864 | 2,863 |
| Missing of 2,880 scheduled 5m bars | 16 | 17 |
| Initial warmup / interrupted 21-bar context | 212 | 232 |
| Stored 1m bars | 14,394 | 14,394 |
| Eligible of 14,400 scheduled minute bars | 14,378 | 14,377 |

Original stored decision hashes were checked before conversion. No official
OHLC fallback, synthetic gap filling, raw tape editing or new canonical ladder
was created. Existing `detect_swings` was reused. Anchor resets prevent carrying
a pre-gap fan into a different closed sequence. Censored paths are unknown,
not zeros; each reserves its full holding interval.

Cost is the frozen `delta_scalp_v2`: 11.8 bps fee/GST + 6 bps friction = 17.8,
subtracted once. Funding is excluded. BBO, market depth, queue, account sizing,
risk approval and actual exchange fills were not replayed. None of these
outcomes are operational shadow orders or profit evidence.

## Validation and evidence

- Nine synthetic tests passed: positive long/short crossings, one episode per
  anchor, prefix causality, gap rejection, forming-candle refusal, prior-volume
  threshold, reaction delay, horizon, cost-once, censor/non-overlap and overwrite
  refusal (some tests cover multiple checks).
- Independent [verify.py](verify.py) passed: source/input hashes, saved anchor
  highs/lows and confirmation timing, ATR scale, line equations, closed crossing,
  volume threshold, episode identity, entry/exit prices and net arithmetic.
- [Frozen results](attempt_01/results.json) retain all measured/censored events,
  source/input fingerprints, summaries and gate counts.
- [Verification record](attempt_01/verification.json) hashes the unchanged results.
- Full existing workspace suite: **3,026 passed, 6 skipped**. The nine new
  research checks run separately because repository default discovery includes
  `tests/`, not report-local tests. This is local validation, not a deployment.

The full input export and original source paths in the evidence are local
provenance references, not bundled data. Reproduction requires those exact
hash-matching inputs, not today's changing VM lake. The run directory refuses
overwrite. Synthetic checks can be repeated without rerunning market research:

```sh
.venv/bin/python -m pytest -q research/reports/gann_slope_20260913/test_screen.py
```

## Decision

No tradable edge demonstrated. Retain the failed screen; do not register,
activate, tune or deploy this experiment. A different anchor, slope, horizon
or exit policy is a new research contract, not a repair to this result.

`can_trade=false`, `can_promote=false`, `performance_eligible=false`.
No operational module, roster, capital permission or VM service changed.
