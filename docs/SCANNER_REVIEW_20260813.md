# Scanner Code-Flow Review — flaws + the fix

**Date:** 2026-08-13 · **Reviewer stance:** trade-analyst, capital-protection first.

Reviewed families: **FVG / liquidity** (`fvg_liquidity_breakout`), **Luxara stacks**
(`luxara_break_bounce_v27`, `luxara_live_plan_qtm`, `luxy_ut_bot_forecast`),
**confluence** (`alpha_stack`, `quant_signal_pack`, `momentum_cascade_lyro`,
`alpha_distillation_pack`).

## The flaws are structural, not bad-luck OOS

| Flaw | Evidence in code | Why it kills edge |
|------|------------------|-------------------|
| DOF explosion | FVG alone: ATR, FVG TTL, body %, volume z, room bps, dual ER, bias EMAs, stop mults, fee fields | fits history; sealed tails fail |
| Geometry ≠ mechanism | FVG / liquidity pools describe *where* price was empty | no durable "who pays whom" |
| Confluence illusion | `long_score >= min_score` from many correlated flags | fake confidence, few independent trades |
| Private cost math | FVG embeds `taker_entry_bps`, `min_expected_net_edge_bps` | diverges from the shared `CostModel` |
| Unitless thresholds | `min_score: 5.0` — not bps, not probability | no link to round-trip cost |
| No capital guard | **`strategy_registry.STRATEGIES` holds all 23 as equally live; `get_strategy_class` hands any of them to the lane factory** | a single roster edit could deploy the zoo with capital |

The last row is the operative risk and the one worth fixing in code. The rest are
reasons these families should never be *refactored in place* into production.

## The fix applied — a guard, not a rewrite

- `strategy_registry.RESEARCH_ONLY` (derived from the class ids, so it can't
  drift) + `is_capital_eligible(strategy_id)`.
- `LaneSpec.capital_downgraded()` applied at the **final roster build**
  (`multi_lane_shadow`): a research-only family is downgraded from any PAPER
  capital lane to SHADOW. `_paper_observation` mirrors (PAPER-mode but non-capital)
  are left untouched. This is a distinct, class-derived layer *beside* the existing
  evidence-prune, so the zoo cannot reach capital by any roster path.
- Result: these families stay importable for research and can run a SHADOW lane
  for observation, but can never back capital. No file deleted, no entanglement
  (`QuantSignalPackParams` dependents) touched — the safety goal is met without
  the archive-by-deletion the entanglement was blocking.

## What was deliberately NOT done

- **No in-place refactor** of FVG/Luxara/confluence internals. Shrinking a 20-knob
  Pine cluster to "cleaner code" recreates the zoo with the same DOF — that is
  drift, not a fix.
- The only legitimate revival is the **G1 pattern**: a NEW ≤5-param locked
  pre-registration of a single economic mechanism, on the TradePlan contract with
  the shared CostModel + `plan_gate`, judged once on a sealed tail. `funding_extreme_fade_short_v2`
  just ran that and FAILED honestly — that is the bar.

## Bottom line

These are sophisticated **pattern engines**, not incomplete premia. Their flaws
are architectural. The code fix is **inertness + capital-ineligibility**, keeping
them as research records; the edge fix, if ever, is a fresh locked mechanism —
never a rewrite of the existing knobs.
