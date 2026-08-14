# Order-flow / CVD edge — the cost wall verdict (EXPLORATORY)

**Date:** 2026-08-14 · **Status:** exploratory read of existing flow research · **Verdict: real micro-edge, ~25× below the taker cost wall**

## Context
Asked to "grow the edge" with a new generalizable signal, the natural candidate was
microstructure order-flow / CVD. A survey found the infrastructure **already
exists and is complete**: aggTrades backfill (`data/aggtrades_backfill.py`), a live
tick recorder, a CVD engine (`research/orderflow_footprint.py`: buy/sell volume,
delta, `cvd_volume`, `cvd_notional_usd`), a tick-lake loader, and a forward-
expectancy miner (`research/scalper_edge_miner.py`, orchestrated by
`l2_research_loop.py`). The VM holds **~14 GB of real recorded tick data**
(binanceusdm/bybit/delta_india × BTC/ETH/SOL/DOGE/BNB/XRP, ~2026-07-04→08-02).

So the question was not "can we build a CVD scanner" (it exists) but "does the flow
signal actually carry a *net* edge?" The miner had already answered it.

## Result (`research/live_research/l2_latest.json`, 900 hypotheses, day 20260802)

| Metric | Raw (pre-cost) | Net (after cost) |
|---|---|---|
| Median edge | **+0.32 bps** | **−7.68 bps** |
| p90 edge | +0.81 bps | −7.19 bps |
| Positive | **836 / 900 (93%)** | **0 / 900 (0%)** |

Net edge by flow family (all negative, uniform):
- absorption_reversal: −7.71 bps (n=171)
- microprice_continuation: −7.65 bps (n=499)
- pressure_continuation: −7.69 bps (n=230)

Route decisions: **BLOCKED = 900 / 900**. Primary blocker for **all 18 lanes:
`NEGATIVE_EDGE_AFTER_COST`**. `edge_candidates: 0`.

## Reading
- **The signal is real.** 93% of hypotheses have positive *raw* forward edge — order
  flow does predict short-horizon returns. This is not noise like crypto_trend's
  cross-symbol failure; the predictive content exists.
- **But it is ~25× too small.** The ~8 bps taker cost wall (≈5 bps taker fee +
  ≈3 bps spread/slippage) dwarfs the ~0.3 bps median edge. Even the p90 best
  hypothesis (0.81 bps) is ~10× under the wall. Zero survive, across every family.
- **This is the recurring theme, quantified.** funding_mr (fee-sensitive, failed
  forward), crypto_trend (thin, didn't generalize), and now flow (0.3 bps vs 8 bps
  cost) all die at the same wall: **thin directional edges cannot pay taker cost**
  at a <$1k retail fee structure.

## The only viable reframe: earn the spread, don't pay it
Every dead edge here is a **taker** strategy. The one lever that moves the ~8 bps
wall is **maker execution** — post-only limit orders that collect the spread (and
maker rebate) instead of crossing it. That could cut the wall from ~8 bps toward
~1–2 bps. But note: even then, the **median 0.3 bps raw edge still does not clear a
1–2 bps maker cost**, and only the p90 tail (0.81 bps) gets close. So maker
execution is necessary but likely **not sufficient** on flow alone — the honest
implication is a **liquidity-provision / market-making posture** (earn spread as the
primary P&L, use flow only to skew/avoid adverse fills), not flow-as-a-directional-
signal. The repo already leans this way in `leadlag_echo_scalp.py` (maker-first) and
the `replay_backtester` maker fill model.

## Caveats (honesty)
- **Single day** (20260802); the loop status is `SCALPER_DATA_COLLECTION` (11 lanes
  `record_more`). A multi-day pass would firm this up — but the gap is **structural**
  (a fixed fee+spread wall vs a ~0.3 bps edge), so it is very unlikely to flip: raw
  edge would need to be ~25× larger on other days, implausible.
- Net cost model is taker-only here; maker was not evaluated (route BLOCKED,
  maker_only=0). Quantifying the maker gap per hypothesis is the concrete next test.

## Recommendation
1. **Do NOT build another taker directional scanner** (CVD-divergence, etc.) — it
   will hit the same wall. The CVD engine already exists and the miner already says
   net-negative.
2. If pursuing flow further, the ONE worthwhile test is **maker-viability**: for the
   top raw-edge hypotheses, does p90+ raw edge clear a realistic maker cost
   (fee + queue/adverse-fill haircut)? If not, flow-as-signal is a dead end here.
3. The strategically honest pivot is **maker / liquidity-provision** strategies where
   the spread IS the edge — a different posture from everything tried so far, and the
   only one that structurally beats the cost wall that has now killed three
   directional candidates. Pre-register any such candidate on untouched data across
   symbols, as always.
