# QuantTrade whole scanner/scoring replay

## Result — completed 2026-09-12

**The complete scalp scanner/scoring path does emit entries. This one local
test does not establish a tradable edge.** On September 1–10 Delta data, the
selected entries had negative average 15-minute movement even before costs.
BTC is under-sampled; ETH fails this modeled-cost benchmark. This is not a
verdict on the upstream bot's untested trailing/partial-exit strategy.

| Funnel | BTCUSD | ETHUSD |
|---|---:|---:|
| Scheduled five-minute closes | 2,880 | 2,880 |
| Actual full `analyze()` calls | 2,864 | 2,863 |
| Skipped: current complete candle missing | 16 | 17 |
| Calls with at least one detector hit | 1,821 | 1,828 |
| All emitted signals, including PRE | 104 | 162 |
| PRE warnings only | 84 | 123 |
| Final BUY/SELL signals | 20 | 39 |
| Overlapping entry signals excluded | 4 | 6 |
| Future-path outcome censored | 0 | 1 |
| Independently measured entries | **16** | **32** |

Entry proxy: one minute after decision close. Exit proxy: 15 minutes after
entry. A return here is a side-adjusted price markout, not a leveraged account
return or a venue fill.

| Per measured entry | BTCUSD | ETHUSD |
|---|---:|---:|
| Average gross movement | −0.58 bps | −11.32 bps |
| Average net, modeled 17.8 bps cost | **−18.38 bps** | **−29.12 bps** |
| Net profit factor, fixed-horizon benchmark | 0.12 | 0.18 |
| Net positive outcomes | 12.5% | 21.9% |
| Fee-only sensitivity, 11.8 bps | −12.38 bps | −23.12 bps |
| Stress sensitivity, 23.8 bps | −24.38 bps | −35.12 bps |
| First five days, average net | −20.43 bps (n=11) | −25.92 bps (n=19) |
| Second five days, average net | −13.88 bps (n=5) | −33.78 bps (n=13) |
| Removing the best outcome, average net | −21.27 bps | −33.04 bps |
| Frozen benchmark classification | INSUFFICIENT_SAMPLE | UNSUPPORTED_AT_MODELED_COST |

The five-day blocks are descriptive slices of already-seen data, not OOS tests.
The small, correlated sample does not establish a stable win rate. In
particular, “confidence 80” is not demonstrated to mean an 80% win probability.

### What the scorer selected

Counts below are final BUY/SELL outputs, followed by independent measured
entries after overlap and missing-future exclusions. PRE warnings are not
included. A dash means no measured outcome, not zero return.

| Scanner | BTC entries / measured | BTC average net bps | ETH entries / measured | ETH average net bps |
|---|---:|---:|---:|---:|
| structure_bounce | 8 / 5 | −16.91 | 17 / 13 | −41.84 |
| liquidity_sweep | 6 / 6 | −22.96 | 11 / 10 | −30.44 |
| bb_squeeze | 3 / 2 | −26.56 | 8 / 6 | −10.33 |
| trend_continuation | 1 / 1 | +25.01 | 1 / 1 | +92.36 |
| cvd_divergence | 1 / 1 | −13.05 | 0 / 0 | — |
| rsi_extreme | 1 / 1 | −30.62 | 0 / 0 | — |
| rsi_divergence | 0 / 0 | — | 1 / 1 | −40.72 |
| post_impulse | 0 / 0 | — | 1 / 1 | −73.15 |
| vwap_mean_revert | 0 / 0 | — | 0 / 0 | — |

Every individual scanner sample is insufficient. The two positive
trend-continuation outcomes are **one per symbol**, not a discovered edge or
permission to select that winner after seeing this table. Structure bounce
produced 207 of the 266 total emissions, but 182 of those were PRE warnings.

### Detector coverage

All 18 methods were available; the unchanged upstream router actually called
12. Detector calls/hits may repeat through fallback routes and are **not
independent trades**.

| Detector | BTC calls / hits | ETH calls / hits |
|---|---:|---:|
| liquidity_sweep | 5,222 / 687 | 5,234 / 638 |
| structure_bounce | 5,212 / 503 | 5,192 / 513 |
| rsi_extreme | 4,496 / 190 | 4,467 / 181 |
| trend_continuation | 610 / 8 | 530 / 8 |
| ema_momentum | 616 / 1 | 537 / 1 |
| bos_choch | 4,734 / 5 | 4,710 / 1 |
| cvd_divergence | 3,607 / 217 | 3,514 / 240 |
| vwap_mean_revert | 3,385 / 441 | 3,292 / 465 |
| rsi_divergence | 4,045 / 533 | 4,036 / 508 |
| post_impulse | 134 / 15 | 161 / 15 |
| bb_squeeze | 3,925 / 147 | 3,854 / 146 |
| order_block_entry | 4,298 / 0 | 4,355 / 0 |

Not invoked under this configuration/window: `vwap_bounce`, `supertrend_flip`,
`simple_bias`, `momentum_ride`, `bb_band_walk`, `momentum_surge`. Those six are
**not tested economically**; forcing them on would be another experiment.

### Why most evaluations emitted nothing

Reported no-output reason prefixes were dominated by VETO (BTC 1,422; ETH
1,393), no setup (1,037; 1,018), and near miss (136; 140). EDGE GATE rejected
83 BTC and 67 ETH calls. Only nine calls per symbol reported initial
insufficient-candle warmup. This external run's low entry count is therefore
not explained solely by initial warmup.

These are the upstream status strings, not a reconstructed perfect funnel.
On **13 calls per symbol**, no signal was returned while status still began
with “Signal”. That stale/incomplete status must not be counted as an entry.
No observed detector exception or fatal analyzer error occurred in this pass.

### Verification and limits

- Full pass: **5,727 evaluations**, 32.24 minutes; local analyzer p95 453.9ms.
  This is a local replay measurement, not a VM/live latency guarantee.
- Independent audit passed: 165 upstream Python/YAML fingerprints unchanged;
  every emitted signal matches its evaluation journal; counts, delayed entry,
  non-overlap, cost arithmetic and research-only flags reconcile.
- All 266 emitted signals carry ML verdict **UNREACHABLE**. The sandbox refused
  514 DNS attempts. No deployed ML accuracy or learned calibration was tested.
- Current incomplete parents were skipped. Supplied historical frames still
  contained gaps on 983 BTC and 1,715 ETH calls, including context; the original
  algorithm's handling was preserved and recorded. No uninterrupted-history or
  canonical operational parity claim.
- A fresh-state historical replay is not a deployed-state replay. Missing
  trained state, different local dependency versions, official 4h context, and
  unmodeled actual exits remain explicit limitations.

**Decision:** keep this import out of the operational roster. A full-strategy
profitability claim would require a separately frozen, faithful exit/execution
replay, real model/state artifacts where applicable, and fresh-data validation.
Do not weaken fees, select the one-trade winner, or relabel PRE warnings as
trades to rescue this result. No VM deployment or capital change was made.

Completed [results](attempt_01/results.json) and
[independent verification](attempt_01/verification.json) are retained beside
the full per-evaluation journal.

## Test boundary

This experiment runs the **actual external `ScalpStrategy.analyze()` method**,
not a handwritten approximation and not the scanner-only shortcut. Source is
pinned to `vikkl1990/VNEdge_QuantTrade@8085fc190aab07c6ebc9a7d5b921e9ab9f10a1ea`.
The [contract](CONTRACT.md) was frozen before the historical pass.

The upstream path retains session filters, indicators, regime routing,
structural prefilter, all router-permitted detectors, scanner weighting,
score normalization, confluence bonuses, winner selection, vetoes, EV gate,
fee/duration checks, ML-client failure handling and final signal construction.
The final signal's confidence, stop, targets and metadata are recorded unchanged.
Known upstream defects are not fixed to improve this result.

This is **not the complete upstream trading service**: the separate investment
strategy, account risk/sizing, execution adapters, SignalTracker exits, live
learner feedback, and venue fills are not run. The user-requested scalp scanner
and scoring path is run in isolation.

## Unavoidable differences from its deployed bot

- Fresh checkout, empty historical scanner weights/EV/calibration state. Those
  production artifacts are not in the repository. No favorable values supplied.
- No deployed ML models or historical model responses. Network is denied, and
  the original client's unavailable response is retained. This does **not** test
  learned inference or reproduce a trained live scoring service.
- Historical wall clock is injected at every decision close, so session rules
  and cooldowns do not accidentally use today's time.
- BTC/USDT and ETH/USDT are the upstream symbol aliases. Prices are from Delta
  India BTCUSD/ETHUSD, not Binance USDT candles.
- Decisions use verified canonical minute data and complete-child 5m/15m/1h
  rollups. Four-hour inspection context is explicitly official Delta OHLC from
  the exported cache, never relabeled canonical trade data.
- Missing current decision parents skip evaluation. Historical missing slots
  remain absent, preserving the legacy algorithm's gap treatment. Every call
  records gap intervals and frame lengths; canonical live parity is not claimed.
- The latest 5,000 rows per timeframe match the upstream store's default limit.
  Indicators are recomputed by upstream code, including its seed/window behavior.

## What the outcome numbers mean

Only final **BUY/SELL** signals enter the independent return benchmark. PRE_BUY
and PRE_SELL are reported separately. A detector hit is not a final entry signal;
fallback calls can check a detector more than once at the same evaluation.
This BUY/SELL restriction is the frozen benchmark's use of `Signal.is_entry`,
not a claim that the external orchestrator never forwards a PRE signal to its
tracker. Its separate post-signal routing is outside this experiment.

The benchmark is deliberately the same as the previous burst-response screen:
entry at the minute open one minute after decision close, exit 15 minutes later,
one non-overlapping position per symbol, 17.8bps modeled full taker costs. Fee-only
11.8 and stress 23.8 sensitivities are fixed in advance. Missing future paths are
censored rather than filled at a fabricated price.

These are **fixed-horizon markouts, not the upstream bot's strategy PnL**. The
emitted stops and partial targets are retained for inspection but not applied;
neither its trailing exits nor maker fills are modeled. No leverage, sizing,
funding or account equity curve. The original exit strategy needs a separate
faithful execution replay before any profitability verdict about the whole bot.

September 1–10 is already-seen exploratory data. No untouched OOS judgment or
promotion. Fewer than 30 measured entries is explicitly insufficient evidence.

## Artifacts and reproduction

- [Harness](run_replay.py), [harness tests](test_harness.py).
- `attempt_01/evaluations.jsonl`: every completed call, final reason, detector
  hits, returned signals, original status, history lengths and gap counts.
- `attempt_01/upstream.log`: warnings and original network/model failure paths.
- `attempt_01/results.json`: completed-run counts, all signals, individual
  benchmark events, per-scanner summaries and source/input fingerprints.

Use a **fresh isolated checkout** at the pinned commit. Existing storage or an
existing attempt journal makes the harness refuse to overwrite/reuse the run.

```sh
.venv/bin/python research/reports/quanttrade_full_stack_20260912/run_replay.py --repo /absolute/path/to/fresh/pinned/checkout
.venv/bin/python -m pytest -q research/reports/quanttrade_full_stack_20260912/test_harness.py
```

Inputs are the temporary static export `/tmp/vnedge-htf-recheck.PInWIi`; its
fingerprints do not replace the original data files. Preserve those files or
re-export exact matching inputs to reproduce. Random upstream trade IDs are not
used to assess decision parity.

Four harness tests pass: numeric complete-child rollup, causal context cutoff,
delayed markout/censor handling and historical clock. Ruff passes. VNEDGE full
regression: **2,993 passed, 6 skipped**, 611 dependency warnings, 184.35 seconds.
Prior external indicator/fee review: **50 passed, 1 failed** (RSI rising-series
NaN); that upstream bug is preserved, not quietly repaired.

No VNEDGE scanner, registry, roster, capital permission or VM service changed.
No orders submitted. `can_trade=false`, `can_promote=false`,
`performance_eligible=false` throughout.

Runtime is the local workspace environment: Python 3.13.3, pandas 3.0.5,
NumPy 2.5.2, Pydantic 2.13.4, PyYAML 6.0.3 and requests 2.34.2. These are
not all the external repository's pinned versions; environment-level production
parity is not asserted. No dependency installation or upgrade was performed.
