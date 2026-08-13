# VNEDGE Full Code Audit — 2026-08-13

Read-only audit of `main` (`a0c5a13`) across four dimensions (execution paths,
UI duplication, config single-source-of-truth, gaps). Focus: **gaps and multiple
divergent code paths**. Nothing was changed during the audit.

## The shape of the problem

The system has grown **parallel paths**: observe-only overlays running beside the
real path, two UIs, four cost sources, four runner classes. **Most are intentional
staging** (D-lite overlays, ML deferred) and are safe today. But several carry real
drift or pre-live safety risk. **Two structural fixes dissolve the majority:**

- **Unify the live vs paper/backtest exit engine** (one shared position/exit manager).
- **Push band/chip/threshold computation server-side into the snapshot** (single
  source = `latency_thresholds.py`, the same code the arm-gate uses).

---

## P0 — safety / pre-live (fix before the live gates are ever opened)

**A1. The real live exit engine is NOT the one you validate.**
`LiveTraderSession` (`runtime/live_trader.py:271-298`) closes positions with a bare
bar-close stop/TP — **no** ATR trailing stop, **no intra-bar tick-stop**, **no**
`max_holding` time cap, **no** `ProtectionState`. Paper/shadow (`live_paper.py`,
`ActiveExitState.resolve_bar` + `_check_tick_stop`) and the backtester all use the
full machinery. So a strategy promoted through backtest→paper→shadow trades on
**different, weaker** exit logic in live. Latent (live gates closed) but a trap for
whoever opens them. *Compounded by* the exit-submission plumbing being
copy-duplicated between `live_trader.py:184-242` and `live_paper.py:1101-1218` and
already drifting (live_trader lacks the reconciliation-aware bookkeeping).
→ **Fix:** extract a shared exit/position manager both sessions embed.

**A2. Position reconciliation silently skipped on account-read failure.**
`live_trader.py:335` `_reconcile_positions()` catches all exceptions, logs, and
`return`s — a persistently failing `account_state()` read disables the venue-is-truth
divergence check **every pass while entries keep flowing**. Unlike the order-reconcile
sibling (which stays blocking via TIMEOUT_UNKNOWN), this has no compensating block —
brushes the "reconciliation ⇒ fail closed" invariant.
→ **Fix:** count consecutive read failures, force reduce-only after N.

---

## P1 — drift risk (operator misled / research ≠ paper ≠ live)

**B1. Cost model is a dashboard overlay, not the live gate.**
`CostModel`/`plan_gate` are observe-only in `live_paper` (`_record_overlays`,
`:494-531`); the real entry path is `strategy.signal → size_position → gateway` with
**no cost gate**. Meanwhile **9 legacy strategies carry private fee constants**
(safety 5; `quantified_fee_wall_sniper` 8; single-sided slip), and **backtest**
(`backtest/fee_model.py`, `slippage_model.py`), **paper** (`paper/fill_model.py`), and
**research** (`scalping/parameter_registry.py` per-venue table, **bybit taker 5.5**)
each keep their own — **four disagreeing cost sources**. *Nuance:* the two live lanes
(funding_mr, crypto_trend) use none of these cost gates, and most of the 9 are now
capital-ineligible via the scanner guard — so the **live** blast radius is small; the
harm is (a) the dashboard shows a cost verdict that isn't the one firing, and (b)
research/paper/backtest silently diverge.
→ **Fix:** source strategy/backtest/paper costs from `CostModel.for_profile(lane)`, or
promote `plan_gate` to a real filter in `_submit_entry`; add bybit's 5.5.

**B2. Latency thresholds triplicated, and the Python copy is the true arm-gate.**
`runtime/latency_thresholds.py` (`TM_AGE_SOFT/HARD`, used at `live_paper.py:486` for
the fail-closed block-new-arms decision) is hand-copied into classic JS
(`index.html:1559-1560`) and React TS (`Panels.tsx:56`, **SOFT only — no HARD table**).
Already divergent: `TM_AGE_HARD_P99_MS={"1m":3000}` is in neither UI. **A UI can show
green while the bot is fail-closed blocking arms** — operator could misread "safe to arm."
→ **Fix:** classify server-side (`classify_tm_age`), ship the **band** in the snapshot
next to the raw value (like `time_machine.health` already does).

**B3. Status-chip / DD-band / lane-synth logic duplicated JS↔TS, already divergent.**
`renderStatusStrip` vs `computeChips`; `ddBand`/`renderTrialDesk` vs the DD/Trial
columns; `_laneRows` in both (plus a 3rd inline synth in `renderCandlePath`). Divergences
already present: classic rolls per-lane `feed` into the FEED chip, React doesn't;
DECISION hard-leg (>200ms) missing in React; DD `limit==null` → green (classic) vs grey
(React); verdict tones differ. **Two cockpits can show different SYSTEM/FEED/DD health
from the same snapshot.**
→ **Fix:** emit a `chips` block + per-lane `bands`/`verdict_tone` server-side; both UIs
become dumb band→color mappers. (Same fix as B2, one move.)

**B4. React `/journal` panel 404s (concrete shipped bug).**
`frontend/src/queries.ts:27` fetches `/journal?limit=`; the server route is
`/trade-journal` (`app.py:971`), which classic uses. React's JournalPanel cannot load.
→ **Fix:** point the query at `/trade-journal` (+ map its response shape).

---

## P2 — latent / lower (mostly intentional; track, don't rush)

- **C1. plan/ enforcement engines built + tested but observe-only.** `EntryEngine`,
  `ExitEngine`, `PlanStrategy`, `plan/builders/*` have zero runtime references
  (tests only). Intentional staging — the D-lite overlay counters (`plan_gate_rejects`,
  `regime_would_block`) are the evidence base. Needs an explicit promotion go/no-go or
  it rots. `regime_v0` and `ml/decision_engine`, `ml/promotion`, the whole `ml/` layer
  are likewise deferred by design (consistent with CLAUDE.md).
- **C2. `PromotionService` ("only legal route research→paper→live") has no caller**,
  while a parallel promotion path already runs (`walk_forward.PromotionGates`,
  `paper_promotion_bridge`). Two routes, one unwired — a foot-gun when the first ML
  model is ready. → reconcile to one route.
- **C3. `exchange/delta_contracts.py:108 size_delta_risk_trade`** is a second sizing
  implementation **missing the liquidation-buffer gate** — research-only today, a live
  landmine if Delta activates through it. (`size_position` remains the single canonical
  sizer for backtest+paper+live — verified.)
- **C4. `strategy/htf_structure_break.py` orphaned** — a pre-registered strategy,
  unregistered, unimported, untested. Dead weight. → register+test or delete + note
  the prereg abandoned.
- **C5. `_trail_atr` (`live_paper.py:737`)** silently returns 0.0 on ATR fault (disables
  trailing that bar, hard stop still fires); no counter. → add a counter for visibility.
  Minor: `pine_replay` hardcodes `<= 30.0` duplicating `ABSOLUTE_MAX_LEVERAGE`.

---

## Verified clean (the invariants that matter hold)

- **Every order passes `PreTradeRiskGateway.evaluate`** — one choke point
  (`OrderManager.submit`); a grep for direct adapter order calls in
  runtime/strategy/scalping returns nothing. No bypass (shadow-prime, tick-stop,
  emergency-flatten all verified).
- **Reduce-only exits never blocked by entry checks** — structural (all entry gates
  sit inside `if not intent.reduce_only:`; exits run before/independent of the arm-gate).
- **`size_position` is the single sizer** (backtest+paper+live share it); **`risk_config`
  limits single-source**; **`regime_v0` imports `add_regime_columns`** (no threshold drift).
- **RESEARCH_ONLY capital guard is actually enforced** (`multi_lane.py:106`).
- **No untested public risk/execution functions; no bare excepts; no substantive
  production TODOs.** All exception-swallowing is observability/lane-isolation, annotated.

---

## Recommended remediation order

1. **B4** React `/journal` → `/trade-journal` (trivial, ship now).
2. **B2+B3** server-side bands/chips block in the snapshot → deletes the JS/TS
   threshold + chip + DD duplication and closes the UI/bot "safe to arm" mismatch.
3. **A2** consecutive-read-failure → reduce-only in `_reconcile_positions`.
4. **A1** unify the live/paper exit engine (shared exit/position manager) — the pre-live
   blocker; do before any live gate opens.
5. **B1** collapse the four cost sources onto `CostModel` profiles (research/paper/live).
6. **C2/C3/C4/C5** housekeeping as capacity allows.
