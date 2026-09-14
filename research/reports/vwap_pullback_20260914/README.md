# VWAP pullback bounce: 96-cell exploratory test

**No edge established. All 96 cells are insufficiently sampled.** This test
is separate from the prior VWAP consolidation-breakout experiment. It exhausts
the frozen matrix below, not every conceivable parameter, market or clock.

## What was tested

| Dimension | Frozen alternatives |
|---|---|
| Timing | Immediate recovery; three-bar delay control; delayed confirmed 3/3 higher low |
| Volume | None; contraction; expansion; contraction + expansion |
| VWAP touch | Wide +0.50/-0.25 ATR; tight +0.25/-0.10 ATR |
| Exit plan | 1.5R / 15 minutes; 2R / 30 minutes |
| Market | Delta BTCUSD; Delta ETHUSD |

48 distinct research revisions x 2 symbols = **96 cells**. Every cell,
including zero-trade cells and all rejection counts, is retained. No live
strategy ID, scanner gate, roster, capital permission or deployment changed.

Data: September 1–10, 2026, from the same static canonical export. This is
already-seen data, NOT untouched OOS. Exact session quote/base sums, UTC reset,
closed 5m decisions and matching complete 1m children; invalid coverage poisons
the rest of that session. Reused data audit: 2,397 BTC / 2,393 ETH usable
session slots out of 2,880 per symbol, before setup warmup and filters.

## Results: unique opportunities, not pooled variant memberships

| Observation | BTC | ETH |
|---|---:|---:|
| Common pullback/recovery setups | 8 | 12 |
| Immediate executable setups, wide band/no volume filter | 0 | 1 |
| Same setups, delayed three bars | 0 | 0 |
| Same delay, confirmed higher-low filter | 0 | 0 |
| Setups satisfying contraction AND expansion | 0 | 0 |
| Cells with any measured outcome | 0 of 48 | 4 of 48 |

The four nonzero ETH cells are **one shared market episode**, September 4,
10:25 UTC recovery → 10:30 UTC entry. It passes the wide band and contraction
filter; it does not pass the tight band or expansion filter.

| Same ETH episode, alternate exit | After modeled costs, before funding |
|---|---:|
| 1.5R target / 15-minute maximum | +9.16 bps |
| 2R target / 30-minute maximum | +2.22 bps |

Repeating it with/without contraction gives four cell results, not four
independent trades. This cannot establish expectancy, PF stability, drawdown
control, session advantage or a preferred exit. Every last-three-day reporting
cell contains zero measured trades. No confidence intervals are shown for
these tiny samples; all matched timing-comparison intersections are empty.

## What blocked conversion

**The strong recovery was usually already extended at the actual entry.**
The frozen limit is one prior ATR above the last known session VWAP, using the
adverse-adjusted next-open price. Eight of eight BTC and eleven of twelve ETH
immediate baseline setups exceed it. Their distances range from 1.06 to
3.35 ATR across the rejected entries. It was not a warmup or missing-fill
problem for those candidates.

Waiting did not repair this sample:

- BTC delay control: 4 extended, 3 invalidated by stop touch during the wait,
  1 invalid entry geometry.
- ETH delay control: 9 extended, 3 invalidated by stop touch.
- BTC higher-low branch: 4 lacked the required confirmed higher low,
  3 invalidated during the wait, 1 still extended.
- ETH higher-low branch: 5 lacked the required confirmed higher low,
  3 invalidated during the wait, 4 still extended.

Volume conditions also rarely coincided: BTC has 3 contraction and 2 expansion
setups; ETH has 6 contraction and 3 expansion setups. Neither symbol has a
single setup with both. These are observable candle-notional conditions,
not proof of institutions or aggressive buying/selling.

This exposes tension between **strong closed-candle confirmation** and
**near-VWAP entry** under this numerical contract. It does not establish that
removing the extension cap or waiting for a retest would be profitable. Those
would be new preregistered hypotheses, not edits to this completed experiment.

## Costs and limitations

Frozen delta_scalp_v2: full taker fee 5.9bps including GST per fill notional,
3bps adverse execution per leg embedded in fill prices; nominal 17.8bps
roundtrip. Repeated 6bps-per-leg stress results are retained. No maker,
queue or BBO execution claim. CostGate wall is not subtracted from PnL.

Funding settlements remain unavailable. Final net_bps is null; positive
numbers above are only before-funding simulated results. No sizing, leverage,
portfolio risk or gateway execution is simulated. Unit-notional trade metrics
are not account returns. Economic proof requires new untouched data, settled
funding, sufficient independent episodes and execution validation.

## Evidence and verification

- [Frozen contract](CONTRACT.md)
- [Full 96-cell results, session/regime breakdowns and timing comparisons](attempt_01/results.json)
- [BTC event/rejection records](attempt_01/BTCUSD.events.json)
- [ETH event/rejection records](attempt_01/ETHUSD.events.json)
- [Independent verification](attempt_01/verification.json)
- [Replay harness](replay.py)

Verification checked source/input/artifact hashes, all 96 cell counts,
216 unique variant ARM IDs, 64 pivot timestamp references, exact entry clocks
and cash arithmetic. ARM IDs are ablation memberships, not independent setups.
Nine new regression tests cover prefix causality, strict pivot visibility,
delayed envelope identity, missing future data, wait invalidation, coverage,
actual entry extension, stop-first ties and sparse uncertainty suppression.

No parameter was changed after outcomes. No candidate was promoted or deployed.
