# Edge search and sampled OFI test — 2026-09-10

## Result

**Found a usable L1 measurement dataset, but no profitable edge in this fixed screen.**
The sampled order-flow-imbalance continuation hypothesis lost after modeled costs
in all four evaluated cells. This is a one-day exploratory result, not a claim
that every order-flow strategy is impossible.

| Symbol | Holding horizon | Non-overlapping measurements | Mean spread-crossed gross bps | Mean full-cost net bps | Mean fee-only sensitivity bps |
| --- | ---: | ---: | ---: | ---: | ---: |
| BTCUSD | 60 seconds | 53 | +1.23 | -16.57 | -10.57 |
| BTCUSD | 300 seconds | 25 | +1.36 | -16.44 | -10.44 |
| ETHUSD | 60 seconds | 48 | -0.30 | -18.10 | -12.10 |
| ETHUSD | 300 seconds | 24 | -6.15 | -23.95 | -17.95 |

These are hypothetical quote-based return measurements, **not fills, booked PnL,
or a new scanner**. Horizons share a day and underlying data; do not pool them
as independent trades. The 5-minute cells are especially small samples.

## What was tested

Past-minute best-level OFI, normalized by average queue depth. Morning observations
set a fixed 90th-percentile magnitude threshold separately for BTC and ETH.
Afternoon observations test the same-sign follow-through. Entry is the first
valid quote at least 250ms after minute close; horizons are fixed at 60 and 300s.
Each symbol/horizon admits only non-overlapping measurements. No parameter search,
sign reversal after seeing losses, leverage, sizing or maker-fill assumptions.

The full [contract](CONTRACT.md) was written before computing outcomes. This is
temporal exploratory validation on September 4 data already inspected for the
range-v2 test, **not untouched OOS judgment**. The minute aggregation is research
measurement only; no operational 1m decision clock was enabled.

Inputs were the 847,070 BTC and 849,033 ETH range-v2 lane-captured BBO rows used
previously. After sequence/freshness/quality filtering, 295,041 BTC and 280,711 ETH
updates remained. Duplicate sequences were not counted as new flow. The screen
retained 1,268 / 1,270 valid minutes; 651 / 652 were calibration minutes.

The feature is based on sampled L1 snapshots, not a complete exchange order-event
stream. The [OFI paper](https://arxiv.org/abs/1011.6402) motivates measuring
best-level changes, but its contemporaneous equity-market result does not prove
predictive Delta profitability. This test explicitly separates past feature time
from future entry and exit.

## Cost interpretation

Gross crosses actual quoted spread: ask-to-bid for longs, bid-to-ask for shorts.
Full-cost net subtracts the repository's `delta_scalp` budget of 17.8bps (modeled
fees/GST plus 3bps extra slippage per leg). The fee-only sensitivity subtracts
11.8bps and removes that extra slippage allowance; **all four means still lose**.

These are repository assumptions, not a fresh account-tariff verification. No
account-dependent close-fee waiver was assumed. Funding, executable order size,
depth consumption, latency beyond the chosen delay, risk approval and kernel
fills were not modeled. No promotion or operational PnL eligibility is implied.

## Data search findings

The following VM stores were checked read-only:

- Current canonical BTC/ETH 15m lake: September coverage, too short for range-v2's
  2,017-bar warmup before the September 4 BBO window.
- BTC/ETH raw trade partition dates: July 11–August 2, then August 31 onward;
  **no August 3–30 partitions** in the inspected tape store.
- Historical range-v2 lane captures: August 31–September 5. Standalone public-book
  recorder partitions begin September 7. They are different capture contracts.
- `research/live_research/revival_lake/.../15m`: 2,829 BTC / 2,825 ETH rows spanning
  July 7–September 5, but a span is not continuous history. These files lack the
  persisted source/hash/coverage/closed-proof columns.
- `data/normalized/.../timeframe=15m`: 17,280 rows per symbol, January 9–July 8;
  no stored canonical proof columns and no September BBO overlap.

No valid long-history range-v2 execution window was found in these stores. No
cross-gap splice, provenance upgrade or official-OHLC substitution was performed.
Unlike range-v2, sampled L1 OFI can be measured from the quote stream itself;
that enabled the concrete, nonzero screen above.

## Arena positive-label audit

The September 9 evidence index lists two unique `strict` candidates, both
`luxara_live_plan_qtm_v1` on SOL 15m, not Delta BTC/ETH:

- Bybit: reported 31 samples, +31.3993 average net bps, PF 2.0975.
- Binance: reported 30 samples, +25.8105 average net bps, PF 1.79.

Each appears in four reporting projections with identical statistics. **Four
reports are not four independent experiments.** Their underlying tournament
labels are maker-route candle outcomes, without demonstrated maker fills here;
source hashes and fee-model strings are empty on the normalized positive rows.
The execution-profile artifact reports zero execution-truth-ready records.
These are old research leads, not a discovered executable scalping edge or grounds
to expand venues/roster. The separate timeframe-model result on the same candidate
rows is negative and must not be silently substituted for the selected-route metric.

## Artifacts and next boundary

- [Frozen measurement contract](CONTRACT.md)
- [Source](screen.py)
- [All four results and individual measurement records](results.json)
- Input shard fingerprints: [prior manifest](../range_baseline_20260910/input_manifest.json)
- Source and contract hashes, timing, non-overlap, counts, modeled-cost arithmetic
  and research-only flags were independently checked against the output.
- Small synthetic sign checks for bid-size increase, ask-size increase and bid
  improvement ran before any data processing.
- Full repository regression: `.venv/bin/python -m pytest -q` — **2,962 passed,
  6 skipped**, 611 dependency deprecation warnings, 178.54 seconds. Passing
  software tests does not turn the measurement into economic evidence.

Do not deploy OFI chasing from this screen or invert it merely because it lost.
A subsequent mechanism (for example absorption rather than continuation) needs
its own frozen claim and recorded-trade test. That is a different hypothesis,
not a discovered edge. Current scanner logic, roster, permissions and VM services
remain unchanged. All outputs have `can_trade=false`, `can_promote=false` and
`performance_eligible=false`.
