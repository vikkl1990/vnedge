# Finding a scalping edge: evidence review and a new fixed screen

2026-09-12 · Delta India BTCUSD / ETHUSD · research only

## Outcome

**No supported edge found in this run.** A high-volume five-minute move, followed
either in its direction or against it, did not produce enough subsequent return
to cover modeled costs. This is a rejection of these two frozen claims on this
window, not proof that all scalping or every volume-based strategy is impossible.

| Symbol | Claim | Measured opportunities | Mean gross bps | Mean net bps at 17.8 | Net PF | Fee-only net bps |
|---|---|---:|---:|---:|---:|---:|
| BTCUSD | Continue burst | 92 | −1.01 | −18.81 | 0.12 | −12.81 |
| BTCUSD | Reverse burst | 92 | +1.01 | −16.79 | 0.13 | −10.79 |
| ETHUSD | Continue burst | 93 | −0.43 | −18.23 | 0.15 | −12.23 |
| ETHUSD | Reverse burst | 93 | +0.43 | −17.37 | 0.18 | −11.37 |

The two directions reuse the **same opportunities**, so these are 185 measured
market episodes, not 370 independent trades. BTC and ETH can also be correlated.
These are delayed candle-open response measurements, not actual orders or fills.

Changing the unit from bps to dollars cannot rescue these results. On a fixed
$1,000 entry notional, +1.01 bps is approximately $0.10 gross versus $1.78 modeled
round-trip cost. Notional is not margin, and this example does not recommend size.
Even removing all modeled slippage leaves every cell negative after fees/GST.

## What was actually run

The [contract](CONTRACT.md) was written before outcome computation. Exactly four
cells, no search for a winning threshold after inspection:

- September 1–10 inclusive, ten completed UTC days, previously inspected data.
- Closed five-minute research parents from five eligible canonical minute children.
- Notional ≥2× the prior 12-parent median; body ≥10bps; close in the outer 20%
  of the range in the body's direction. The baseline excludes the decision bar.
- Enter at the minute open one minute after decision close; exit 15 minutes later.
- Continuation and reversal preregistered together. Non-overlap; incomplete future
  observations censored and counted, with their holding interval still reserved.
- No stops, targets, position sizing, leverage, gateway, kernel, funding, actual
  spread, queue model or execution parity. A fixed-horizon mechanism screen
  precedes an order-level backtest; it does not substitute for one.

No operational strategy ID, threshold, roster, capital permission or VM service
was changed. This does not enable the one-minute decision clock in production.

## Data integrity and exclusions

| Check | BTCUSD | ETHUSD |
|---|---:|---:|
| Expected minute slots | 14,400 | 14,400 |
| Stored minute rows | 14,394 | 14,394 |
| Missing minutes | 6 | 6 |
| Stored rows failing quality/coverage | 16 | 17 |
| Eligible minutes | 14,378 | 14,377 |
| Complete eligible five-minute parents | 2,864 | 2,863 |
| Raw setups | 122 | 125 |
| Overlapping setups skipped | 29 | 32 |
| Future-path-censored episodes | 1 | 0 |
| Measured episodes | 92 | 93 |

Stored hashes were recomputed from raw persisted fields before numerical
conversion. Closed/source/time/price-geometry checks also passed on eligible
rows. This verifies the exported bar representation, **not independent raw-print
completeness**, historical publication identity, or current live/replay parity.
The censored BTC event has an unknown return, not a zero; statistics describe
measured episodes only. No gap filling, repaired provenance or official candles.

All input shard SHA256s, contract/code/cost fingerprints, and individual
measurements are retained in [results.json](results.json). Inputs are a temporary
local VM export at `/tmp/vnedge-htf-recheck.PInWIi`; preserve or re-export those
exact files to reproduce later. The manifest is not a substitute for the data.

## Robustness: no hidden positive slice

Both five-day reporting blocks were negative in every cell. They are descriptive
splits of already-seen data, **not untouched OOS**.

| Cell | First five days net bps | Second five days net bps | Descriptive day-bootstrap 95% interval, mean net bps |
|---|---:|---:|---:|
| BTC continuation | −18.63 | −18.97 | [−24.81, −13.66] |
| BTC reversal | −16.97 | −16.63 | [−21.94, −10.79] |
| ETH continuation | −22.32 | −15.15 | [−23.06, −14.40] |
| ETH reversal | −13.28 | −20.45 | [−21.20, −12.54] |

The bootstrap resamples whole days, including no-event days, with a frozen seed.
Ten days are few blocks; these intervals do not establish long-run confidence.
Removing the best observation worsens every cell. The 23.8bps stress sensitivity
also fails. Cumulative net-bps drawdowns in JSON are event-series diagnostics,
**not account percentage drawdowns**.

## What the earlier work adds

- [Sampled L1 imbalance](../ofi_screen_20260910/README.md): same-sign follow-through
  failed all four BTC/ETH 60s/300s cells, including fee-only sensitivity. It used
  sampled lane quotes, not a complete order-event feed. More OFI arrows are not
  established edge.
- [Range-v2 baseline](../range_baseline_20260910/README.md): insufficient verified
  warmup overlapping BBO. Zero arms means **untestable**, not economically rejected.
- [HTF path recheck](../htf_path_recheck_20260912/README.md): continuation remained
  denied on its tested regime. Zero trades provide no expectancy estimate.
- Arena maker-route positive labels reviewed in the OFI report lack sufficient
  execution identity/fill proof. Repeated projections are not independent wins.

Do not collapse economic failure, insufficient data, and legitimate regime denial
into one “scanner broken” category.

## What to investigate next—and why

**My architectural inference:** the next useful question is whether executable
book response distinguishes the rare profitable episodes, not whether another
oscillator can label a candle. This is a hypothesis, not a promised edge.

1. **Verify measurements before signed-flow research.** The current local
   `delta_ws.py:337–340` maps anything other than seller-taker to buy, including
   unknown roles. This identifies a possible contamination path; it does not prove
   unknown roles occurred in this dataset. Audit raw role coverage and represent
   unknowns explicitly before using taker-buy imbalance. This screen avoids it.
2. **One separately frozen book-response hypothesis.** Test whether a verified
   burst of aggressive trades produces sustained depletion of opposing liquidity,
   or rapid replenishment without price progress. Pair trades and lane-consumed
   BBO by their causal receipt clocks, require known sides, valid sequences and
   realistic delay, and measure future ask/bid executable markouts after full
   costs. Replenishment from sampled L1 is only a proxy; claims about cancellations
   or queue priority need richer book events. Do not invent L2 from candles.
3. **Only then consider maker execution.** Require depth/queue evidence, missed
   fills, cancellations, and adverse selection. Lower posted fees alone do not
   prove a cheaper realized trade. Bar-touch is not a maker fill.
4. **Freeze a future untouched window before testing.** Log every attempted claim
   and threshold, including failures. Select one candidate only on exploratory
   evidence; no same-window retuning or automatic promotion. A positive gross
   result below modeled costs is not a candidate for capital.

If the next properly measured claim still cannot pay full costs, the conclusion
should be that the proposed Delta taker scalp lacks evidence—not a request to
lower CostGate. A different venue, fee arrangement or holding clock is a separate
user-approved experiment, not a silent reinterpretation of this one.

## Primary research—not proof of Delta profitability

- Cont, Kukanov and Stoikov studied **contemporaneous** best-book imbalance and
  price impact in US equities; their result motivates recording book changes,
  not assuming a forecast survives a delayed entry on Delta.
  [The Price Impact of Order Book Events](https://arxiv.org/abs/1011.6402).
- Execution choice depends on order flow, queue sizes, and fee/rebate structure.
  That motivates modeling conditional fills, rather than replacing taker cost
  with a maker tariff on the same hypothetical trades.
  [Optimal order placement in limit order markets](https://arxiv.org/abs/1210.1625).
- Trying many configurations and retaining only winners can manufacture apparent
  backtest success. This run fixes its trial budget and keeps every cell.
  [Statistical Overfitting and Backtest Performance](https://sdm.lbl.gov/oapapers/ssrn-id2507040-bailey.pdf).

## Reproduction and verification

```sh
.venv/bin/python research/reports/burst_response_20260912/screen.py --input /tmp/vnedge-htf-recheck.PInWIi
.venv/bin/python -m pytest -q research/reports/burst_response_20260912/test_screen.py
```

Seven targeted tests cover causal reference windows, missing/invalid children,
entry delay, holding horizon, competing signs, censored non-overlap, cost
arithmetic and repeatable summaries. Ruff passes. Full repository regression:
**2,993 passed, 6 skipped**, 611 dependency deprecation warnings, 187.98 seconds.
Independent output checks confirmed fingerprints, counts, paired directions,
net arithmetic and research-only flags. Software tests are not edge evidence.

Every output remains `can_trade=false`, `can_promote=false`,
`performance_eligible=false`. No orders, no deployment, no strategy promotion.
