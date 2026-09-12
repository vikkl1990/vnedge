# Range break / retest — isolated scalp hypothesis

Status: BUILT FOR RESEARCH, not registered, not rostered, no execution authority.
Strategy ID: `range_break_retest_5m_v1`. Universe: Delta India BTCUSD / ETHUSD,
separate symbol scoreboards. Specification frozen before any market replay.

Claim: after a closed five-minute break of a pre-existing 20-bar range, the
immediately following closed bar retests that boundary and holds outside it;
the subsequent move may continue far enough to cover full taker costs.
This is a hypothesis, not evidence of liquidity, order flow, or positive expectancy.
It is not the previous burst-response detector and is not selected because the
external trend-continuation scanner happened to win one trade per symbol.

## Skeleton retained

`analyze -> validate -> detect -> score -> veto -> SignalIntent`

The implementation retains that interface/flow, not the external repository's
18 detectors, opaque score bonuses, ML fallback, or mutable learned state. It
also implements VNEDGE's `prepare / signal / evaluation_diagnostics` contract.
No dependency on a temporary external checkout. No edits to old strategy IDs.

## Frozen rules

- Clock: closed 5m; next 5m open entry. No forming or 1m decision clock.
- Required window: 20 range bars + breakout bar + retest/decision bar. All 22
  must be consecutive UTC-aligned, canonical, closed, covered, correctly hashed,
  same exchange/symbol/TF, finite coherent OHLC with positive base volume.
- Range: highest high / lowest low of the 20 bars **before** the breakout.
- Long: breakout opens at/below range high and closes above it. Its body is
  at least 60% of its high-low range. Overshoot is at most 25% of prior range.
  Retest low is within +/-5% of prior range height around the broken high;
  retest closes above the level, above its own open, and in its upper 40%.
- Short: exact mirror at range low, with retest close in lower 40%.
- Retest close must remain within 25% of range height of the broken level.
- Stop: retest low minus 5% of range height (long), or high plus 5% (short).
  Never tighten/clamp the structural stop to force acceptance.
- Target: one prior range height beyond the broken boundary. This is an
  explicit measured-range **target assumption**, never an expected edge.
- Cost: existing `delta_scalp_v2`, full modeled taker cost 17.8bps. Reject a
  changed cost configuration rather than silently changing the experiment.
- Setup geometry only: target room >= 2 x booked cost, and
  `(target_room_bps - booked_cost) / (stop_distance_bps + booked_cost) >= 1.5`.
  These are not CostGate approval and cannot substitute for OOS expectancy.
- Research exit contract: structural stop or target, stop-first same-bar tie;
  otherwise 3 completed 5m bars after entry (15m). Actual-entry gap checks,
  fees, slippage, funding and this exit contract need faithful execution replay.
  This module does not implement fills, timers, partial exits or trailing exits.

## Scoring and evidence

Three disclosed ranking components: breakout overshoot strength (0–30), retest
close location (0–40), net reward/risk geometry (0–30). Sum 0–100, not a win
probability. No score threshold, dynamic bonus, RSI/MACD vote, ML vote, or
outcome-fed tuning. Gates are listed separately and all computable failures
are returned. A failed setup has no candidate even if its score is high.

Every candidate has the existing decision-bar-only FrozenPermissionSnapshot
and DecisionEnvelope; no fabricated HTF references. The research wrapper stores
all 22 input hashes, range bounds, breakout identity, stop/target, episode ID,
spec hash and cost-config hash. A changed input window changes its evidence ID.
The deterministic episode ID names the breakout bar and side. Re-evaluating a
bar returns the same IDs; later bars cannot reuse that breakout as their trigger.
An eventual consumer must deduplicate IDs across retries/restarts; the pure
strategy intentionally keeps no mutable 'already fired' journal.

`expected_gross_edge_bps=None`, `edge_model_id=None`, `can_trade=false`,
`can_promote=false`, `performance_eligible=false`, path `research_observe` on
the research wrapper. The standard envelope's kernel path tag is an identity
contract, not proof of a submitted ManagedOrder. Research rows are not fills.

## Before any promotion

Verify causal prefix parity, gap/hash failure handling, long/short symmetry,
exact level geometry, immutable identities and a real, untouched-data economic
test. September 1–10 was already seen and can only be exploratory. Do not run
protected judgment windows or tune thresholds on them. No claim of edge is
made by building this logic. No live integration or deployment in this slice.

## Local use

Implementation: `src/vnedge/research/range_break_retest.py`. No installation,
external checkout, registry import or venue client is needed.

```python
from vnedge.research.range_break_retest import RangeBreakRetestResearch

engine = RangeBreakRetestResearch("BTCUSD")
evaluation = engine.evaluate_market("BTCUSD", {"5m": canonical_closed_5m_frame})
diagnostics = evaluation.diagnostics()
candidate = evaluation.candidate  # None on any failed setup/input gate
# engine.analyze(...) returns a list of SignalIntent for scanner-style callers.
# Do not interpret that list as order approval or submit it to a venue.
```

The frame requires `timestamp, exchange, symbol, timeframe, open, high, low,
close, volume, candle_source, content_sha256, is_closed, data_quality,
coverage_ok`. Use genuine canonical publisher rows; do not stamp foreign OHLC
with a fabricated canonical source/hash to make the assertion pass. Missing
5m data yields `decision_frame_missing`; every evaluation returns its own
diagnostics, so the prior signal's status cannot linger after a rejection.

Synthetic fixtures exercise long/short behavior and rejection boundaries;
they are software tests, not economic results. At initial build time, no
historical replay had been performed.

Subsequent first replay (2026-09-12): 5 BTC / 6 ETH simulated outcomes on
September 1–10; average net −40.20 / −26.13 bps at 17.8 bps modeled cost.
Both samples are insufficient and economically unfavorable; no promotion.
Full evidence: `research/reports/range_retest_20260912/README.md`.

## Build verification

- Latest focused suite: 33 passed, including foreign-feature isolation for
  permission snapshots, causal prefixes, long/short symmetry, gap/hash/coverage
  rejects, preserved stops, cost drift, duplicate columns and overflow.
- Full regression run: 3,025 passed, 6 skipped (611 dependency warnings).
  The final additional permission-isolation test was run in the focused suite.
- Ruff passes. Existing scanner, registry, roster and VM files were not changed.
