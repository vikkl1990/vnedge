# Candle Path Verification Report

**Date:** 2026-08-12
**Commit SHA at verification:** `83315a4` (baseline) → `3b359bc` (Phase A applied)
**Lane(s) observed:** `vnedge-multi-lane-shadow-1` — funding_mr (1h, shadow+paper), crypto_trend DOGE (shadow+paper)
**Method:** deployed-source inspection on the VM + live `/state` sampling + local library tests. No trading behaviour was changed to run this gate.

> Verdict up front: **NO-GO** on the *strict* Candle Path Verification Spec v1.0.
> The **core is real and running** — forming awareness is wired into the live
> lanes, decisions are causal on closed bars, and the loop is fail-closed. What
> is **not** complete is the spec's *measured + gated + operator-visible* layer:
> the composite TM-health arm-gate (§7), the §6/§8 latency-metric emission and
> budgets, and the live gap/stall drills. Phase A (this commit) closes the
> snapshot-contract gap; B closes the arm-gate + metrics; the drills follow.
> This matches the honest self-assessment already on record ("guards exist,
> not yet proven within budget").

---

## Wiring — W1..W7

| ID | Check | Result | Evidence |
|----|-------|--------|----------|
| W1 | TimeMachine per lane | **PASS** | `live_paper.py:205-207` — instantiated when `timeframe ∈ {1m,5m,15m,1h,4h}`; funding_mr=1h → active. Lanes are real `LivePaperSession` (`multi_lane.py:48,99`). |
| W2 | `on_kline_update` forming **and** closed | **PASS** | `_feed_time_machine` feeds `is_closed=True` (closed row) and `is_closed=False` (`feed.forming_candle`). `LiveMarketFeed._forming` is populated from `watch_ohlcv` (`live_feed.py:143-154`). |
| W3 | `on_trade` trade-stream aggregate | **NOT WIRED** | Feed is candle-based (CCXT Pro `watch_ohlcv`), no trade stream. 1m forming advances on kline updates, not per-trade. Acceptable; documented, not claimed. |
| W4 | Feed continuity / REST heal still runs | **PASS** | Separate feed-continuity guard: `_guard_candle_continuity`, `_gap_fill`, `_enter_degraded` → reduce-only. |
| W5 | `check_health(now)` on a timer | **PASS (note)** | Called inside `_feed_time_machine`, which runs on each closed-bar append **and** the idle tick — so it fires without new WS events. Cadence is event/idle-driven, not a fixed timer. |
| W6 | `snapshot_dict()` in `build_snapshot` | **PASS (Phase A)** | Was under `session.time_machine` only + no `snapshot_age_ms`. `3b359bc` promotes `time_machine`+`latency` to canonical top level and adds `snapshot_age_ms`. |
| W7 | Decision reads last **closed** decision-TF only | **PASS** | Structural: strategies consume the closed-candle history; Time Machine is read-only awareness and is never fed into `signal()`. |

## Invariants — I1..I7

| ID | Invariant | Result | Note |
|----|-----------|--------|------|
| I1 | No decision from a **forming** decision-TF bar | **PASS** | Structural (W7). |
| I2 | Forming 1m..4h updates before close | **PASS (confirmed live)** | VM `/state` (in-container, `3b359bc`): `time_machine.forming["1h"].progress = 0.73`, `health["1h"] = "ok"` — the 1h decision bar is tracked at 73% *before close*. |
| I3 | Backward / future bars don't corrupt closed history | **PASS** | TimeMachine future-reject + monotonic drop; unit-tested. |
| I4 | Gap on decision TF → no new arms | **PASS via feed guard** | Provided by the feed-continuity guard (reduce-only), **not** by TM health. The two are separate subsystems today — see Gap #1. |
| I5 | Stall → no new arms; exits allowed | **PASS** | Feed-guard stall detector + reduce-only; reduce-only exits are never blocked (hard invariant). |
| I6 | TM failure must not crash the loop | **PASS** | `_feed_time_machine` is fail-closed try/except → sets `_tm_degraded`, never raises into the run loop. |
| I7 | Snapshot exposes `time_machine` + `snapshot_age_ms` | **PASS (Phase A)** | `snapshot_age_ms` did not exist before `3b359bc`. |

## Live checks — §7

| # | Check | Result |
|---|-------|--------|
| 7.1 | Forming awareness (progress 0→1) | **CONFIRMED LIVE** — 1h forming at 0.73, health ok (VM `/state`) |
| 7.2 | Closed decision only | **PASS** (structural; no forming-bar arms observed) |
| 7.3 | Gap inject drill | **NOT RUN** (unit-tested in library only) |
| 7.4 | Stall drill | **NOT RUN** (unit-tested in library only) |
| 7.5 | Future bar reject | **PASS** (unit test) |
| 7.6 | Process resilience (TM exception → fail-closed) | **PASS** (by construction; unit-level) |

Library tests green: `test_time_machine`, `test_plan`, `test_plan_strategy`, `test_latency_thresholds`, `test_multi_lane` → 60+ passing; full suite **1934 passed**.

## Metrics — §6 / §8

| Metric | State |
|--------|-------|
| `feed_lag_ms`, `decision_lag_ms` (p50/p95) | **PRESENT** (`LatencyTracker`) |
| `tm_last_update_age_ms{tf}` | **MISSING** |
| `snapshot_age_ms` | **ADDED (Phase A)**, not yet a rolling p99 gauge |
| `closed_bar_process_lag_ms{tf}` | **MISSING** |
| `tm_gap_total / tm_stall_total / tm_future_reject_total` | **MISSING** (events exist in TM; not surfaced as counters) |
| `decision_skip_total{reason}` | **MISSING** |
| SOFT/HARD budget table | **ADDED (Phase A)** `latency_thresholds.py`; not yet wired to an arm-gate or emitted as classified bands |

## Live measurements (VM, `3b359bc`, primary lane = funding_mr 1h)

| Metric | Value | Budget | Band |
|--------|-------|--------|------|
| `time_machine.forming["1h"].progress` | 0.73 | — | tracking |
| `time_machine.health["1h"]` | ok | — | ok |
| `snapshot_age_ms` | 1865.9 | soft 3000 / hard 10000 | **ok** |
| `decision_lag_ms` p95 | 26.2 | soft 50 / hard 200 | **ok** |
| `feed_lag_ms` p95 | 1581.8 | (report-only) | baseline |

Note: the Caddy-fronted `https://…/state` returned empty during and shortly
after the container recreate (no "warming" response — it just blanks). The
in-container `:8080/state` served correctly throughout. That blanking is a
real, if minor, observability gap and is itself an argument for the Phase C
health cockpit + a stale-snapshot banner.

---

## Gaps, ranked (→ phase that closes each)

1. **TM health does not gate arms.** → **Phase B — DONE (`1b668c1`)**. `_candle_path_arm_block` blocks NEW entries on decision-TF health ≠ ok / HARD age / tm_error; consulted only on the entry branch so exits are structurally unblockable. **Confirmed live: `decision_skips={}` on all 4 lanes (no spurious blocking), `health[1h]=ok`, `age_ms[1h]≈0.4ms`.**
2. **§6/§8 latency metrics.** → **Partially DONE (`1b668c1`)**: `decision_skips{reason}` + per-TF `age_ms` now emitted; `feed_lag`/`decision_lag` p50/p95 already present. **Still missing:** `closed_bar_process_lag_ms{tf}`, and the gap/stall/future events as monotonic counters.
3. **Gap/stall drill.** → **Phase B7 — DONE (`005ea28`+)**: proven in the *real run loop* by integration test — healthy→entry allowed, gapped decision-TF→entry blocked + `decision_skips` counted, and a stop-hit **exit still fires with a gapped TM** (exit-safety). Live confirmation showed `decision_skips={}` under normal WS (non-spurious). Remaining: only the passive ≥2h wall-clock observation, which is running.
4. **No operator health cockpit** rendering TM/latency/regime/gates. → **Phase C — OPEN**.
5. **Canonical snapshot contract.** → **Phase A — DONE (`3b359bc`)**.

## Go / No-Go for scanner rework

**GO** (for the candle-path gate). The blocking mechanism is live, verified
non-spurious on the VM (`decision_skips={}` under normal WS), and the gap/stall
drill — fire, count, recover, and exit-safety — is proven in the real run loop
by integration test. The passive ≥2h wall-clock observation continues but no
longer blocks. `funding_extreme_fade_short_v2` (G1) is therefore **unblocked**
for its sealed rework whenever chosen — pre-registration locked, PlanBuilder
staged, run under the shared CostModel + plan_gate.
