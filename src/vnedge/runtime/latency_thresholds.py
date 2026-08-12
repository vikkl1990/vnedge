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
TM_AGE_HARD_LAST_MS: dict[str, int] = {"1m": 5000, "5m": 12000, "15m": 30000, "1h": 90000, "4h": 300000}
TM_AGE_HARD_P99_MS: dict[str, int] = {"1m": 3000}

# --- closed-bar decision path (ms) -------------------------------------------
CLOSED_BAR_LAG_SOFT_P99_MS = 500
CLOSED_BAR_LAG_HARD_P99_MS = 2000
DECISION_COMPUTE_SOFT_P99_MS = 50
DECISION_COMPUTE_HARD_P99_MS = 200

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


def blocks_new_arms(band: Band) -> bool:
    """Arm-gate rule: only a HARD band on the decision TF blocks new arms."""
    return band == "hard"
