"""Canonical latency budgets (SOFT / HARD) for the candle path.

Single source of truth shared by the runtime arm-gate and the dashboard, so the
bot's fail-closed decisions and the UI's colour bands can never disagree (the
"one latency config" rule). Values come from the Candle Path latency addendum
(2026-08-12).

Semantics:
  * HARD breach on the *decision* timeframe blocks NEW arms (fail-closed);
    exits / reduce-only / kill remain active.
  * SOFT breach degrades a badge and may alert; arms still allowed unless a
    HARD rule also fires.
  * REPORT-only metrics carry no auto-block — they exist to baseline budgets.

This module is pure data + classification. It does NOT read snapshots, place
orders, or import runtime state; callers pass measured samples in.
"""

from __future__ import annotations

# --- forming-state age (ms): now - last exchange update for a TF -------------
TM_AGE_SOFT_P99_MS: dict[str, int] = {"1m": 1500, "5m": 3000, "15m": 5000, "1h": 8000, "4h": 15000}
TM_AGE_HARD_LAST_MS: dict[str, int] = {
    "1m": 5000,
    "5m": 12000,
    "15m": 30000,
    "1h": 90000,
    "4h": 300000,
}
TM_AGE_HARD_P99_MS: dict[str, int] = {"1m": 3000}

# --- closed-bar decision path (ms) -------------------------------------------
CLOSED_BAR_LAG_SOFT_P99_MS = 500
CLOSED_BAR_LAG_HARD_P99_MS = 2000
DECISION_COMPUTE_SOFT_P99_MS = 50
DECISION_COMPUTE_HARD_P99_MS = 200
# Strategy compute budgets scale with the decision horizon.  Applying the 5m
# scanner's 200ms hard ceiling to a causal 15m structure build made healthy
# pandas work fail closed even though the candle itself arrived on time.  The
# short-horizon default remains deliberately strict; only slower decision
# horizons receive the wider, still-bounded budget below.
DECISION_COMPUTE_LIMITS_MS: dict[str, tuple[int, int, int]] = {
    # timeframe: (soft p95, hard p95, hard-gate recovery sample)
    "15m": (500, 1000, 750),
    "1h": (750, 2000, 1500),
    "4h": (1500, 4000, 3000),
}
# A p95 arm gate needs at least 20 observations (one 5% tail sample). Before
# then the metric is visible but statistically immature and cannot halt arms.
LATENCY_GATE_MIN_SAMPLES = 20
# A rolling p95 deliberately remembers bad history, but it must not leave a
# recovered stream arm-blocked for most of a trading day.  A HARD gate clears
# only after this many distinct, newly recorded observations are all inside a
# conservative recovery budget.  History remains SOFT/degraded until the p95
# itself cools below HARD.
LATENCY_RECOVERY_CONSECUTIVE_SAMPLES = 5
CLOSED_BAR_LAG_RECOVERY_MS = 1500
DECISION_COMPUTE_RECOVERY_MS = 100

# --- snapshot / UI path (ms) — observability, never gates arms ---------------
SNAPSHOT_AGE_SOFT_P99_MS = 3000
SNAPSHOT_AGE_HARD_P99_MS = 10000

# --- feed continuity (aligned with the existing feed guard + Time Machine) ---
FUTURE_TOLERANCE_MS = 2000
GAP_MULT = 1.5
STALL_MULT = 2.5
HEAL_TIMEOUT_MS = 10000
PERSIST_DEGRADE_MS = 60000

Band = str  # "ok" | "soft" | "hard" | "unknown"


def decision_compute_limits(timeframe: str) -> tuple[int, int, int]:
    """Return SOFT/HARD/recovery decision budgets for ``timeframe``.

    Unknown and short timeframes retain the conservative 50/200/100ms
    defaults.  A missing configuration can therefore never silently widen a
    new scanner's arm budget.
    """
    return DECISION_COMPUTE_LIMITS_MS.get(
        str(timeframe or "").strip().lower(),
        (
            DECISION_COMPUTE_SOFT_P99_MS,
            DECISION_COMPUTE_HARD_P99_MS,
            DECISION_COMPUTE_RECOVERY_MS,
        ),
    )


def classify_tm_age(tf: str, last_ms: float | None, p99_ms: float | None = None) -> Band:
    """Classify a forming-state age sample for timeframe ``tf``.

    ``last_ms`` is the most-recent age sample; ``p99_ms`` (optional) the rolling
    p99. Returns the worst band that applies. Unknown TFs (no budget) never
    escalate past ``ok`` so an unconfigured TF cannot silently block arms.
    """
    if last_ms is None:
        return "unknown"
    hard_last = TM_AGE_HARD_LAST_MS.get(tf)
    if hard_last is not None and last_ms > hard_last:
        return "hard"
    hard_p99 = TM_AGE_HARD_P99_MS.get(tf)
    if p99_ms is not None and hard_p99 is not None and p99_ms > hard_p99:
        return "hard"
    soft = TM_AGE_SOFT_P99_MS.get(tf)
    reference = p99_ms if p99_ms is not None else last_ms
    if soft is not None and reference > soft:
        return "soft"
    return "ok"


def classify_p99(value_ms: float | None, soft_ms: float, hard_ms: float) -> Band:
    """Generic SOFT/HARD classification for a single p99 gauge (closed-bar lag,
    decision compute, snapshot age)."""
    if value_ms is None:
        return "unknown"
    if value_ms > hard_ms:
        return "hard"
    if value_ms > soft_ms:
        return "soft"
    return "ok"


def recovery_tail_count(stats: object, recovery_ms: float) -> int:
    """Count consecutive healthy observations at the end of ``stats.recent``."""
    if not isinstance(stats, dict):
        return 0
    recent = stats.get("recent")
    if not isinstance(recent, list):
        return 0
    count = 0
    for raw in reversed(recent):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            break
        if value > recovery_ms:
            break
        count += 1
    return count


def latency_recovery_state(
    stats: object,
    *,
    soft_ms: float,
    hard_ms: float,
    recovery_ms: float,
) -> dict[str, object]:
    """Return the raw/effective band and bounded recovery proof.

    The gate trips from the mature rolling p95.  If that p95 is still HARD,
    five fresh samples under ``recovery_ms`` downgrade the *operational* band
    to SOFT.  This restores new-arm evaluation while keeping the old tail
    visible as degraded telemetry.  Any later sample over the recovery budget
    resets the proof and makes the HARD p95 block again.
    """
    if not isinstance(stats, dict):
        return {
            "state": "unknown",
            "raw_band": "unknown",
            "effective_band": "unknown",
            "healthy_samples": 0,
            "required_samples": LATENCY_RECOVERY_CONSECUTIVE_SAMPLES,
            "recovery_threshold_ms": recovery_ms,
        }
    try:
        samples = int(stats.get("n") or 0)
        p95 = float(stats.get("p95"))
    except (TypeError, ValueError):
        samples, p95 = 0, 0.0
    if samples < LATENCY_GATE_MIN_SAMPLES:
        return {
            "state": "warming",
            "raw_band": "unknown",
            "effective_band": "unknown",
            "healthy_samples": 0,
            "required_samples": LATENCY_RECOVERY_CONSECUTIVE_SAMPLES,
            "recovery_threshold_ms": recovery_ms,
        }
    raw_band = classify_p99(p95, soft_ms, hard_ms)
    healthy = recovery_tail_count(stats, recovery_ms) if raw_band == "hard" else 0
    recovered = raw_band == "hard" and healthy >= LATENCY_RECOVERY_CONSECUTIVE_SAMPLES
    effective_band = "soft" if recovered else raw_band
    state = (
        "recovered"
        if recovered
        else "recovering"
        if raw_band == "hard" and healthy > 0
        else "blocked"
        if raw_band == "hard"
        else "nominal"
    )
    return {
        "state": state,
        "raw_band": raw_band,
        "effective_band": effective_band,
        "healthy_samples": healthy,
        "required_samples": LATENCY_RECOVERY_CONSECUTIVE_SAMPLES,
        "recovery_threshold_ms": recovery_ms,
    }


def classify_latency_stats(
    stats: object,
    *,
    soft_ms: float,
    hard_ms: float,
    recovery_ms: float,
) -> Band:
    """Recovery-aware band shared by the arm gate and both dashboards."""
    return str(
        latency_recovery_state(
            stats,
            soft_ms=soft_ms,
            hard_ms=hard_ms,
            recovery_ms=recovery_ms,
        )["effective_band"]
    )


def blocks_new_arms(band: Band) -> bool:
    """Arm-gate rule: only a HARD band on the decision TF blocks new arms."""
    return band == "hard"
