"""Server-side health bands + status chips — the ONE place UI colours are decided.

Both cockpits (classic + React) render these bands directly instead of
re-implementing the budgets client-side, so a UI colour and the fail-closed
arm-gate (which classifies with the SAME ``latency_thresholds``) can never
disagree. This replaces the triplicated ``TM_AGE_*`` / ``computeChips`` / ``ddBand``
logic that lived in Python + classic JS + React TS.

Pure functions over an assembled snapshot dict; no I/O, no mutation of inputs
except the explicit ``annotate`` helper.
"""
from __future__ import annotations

from collections.abc import Mapping

from vnedge.runtime import latency_thresholds as LT

# arm-gate bands -> UI bands (ok/degraded/blocked/unknown)
_BAND = {"ok": "ok", "soft": "degraded", "hard": "blocked", "unknown": "unknown"}
_RANK = {"blocked": 3, "degraded": 2, "ok": 1, "unknown": 0}

_CANDLE_ARM_BLOCK_REASONS = (
    "canonical_bar_timeout",
    "decision_tf_stale",
    "decision_tf_gap",
    "candle_gap",
)


def _worse(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def _skip_count(o) -> int:
    return sum(int(v) for v in o.values()) if isinstance(o, dict) else 0


def _blocks_candle_path(lane: Mapping[str, object]) -> bool:
    """Return whether the current arm block is a candle-integrity failure.

    Bar-close processing and strategy-compute latency block new decisions, but
    they do not make a fresh candle stale. Keeping those states separate stops
    the lane table from rendering the contradictory ``degraded · 0s`` candle
    cell while the operational HEALTH column still reports the arm block.
    """
    values = (lane.get("arm_blocked"), lane.get("degraded"))
    text = " ".join(str(value or "").lower() for value in values)
    return any(reason in text for reason in _CANDLE_ARM_BLOCK_REASONS)


def lane_rows(snap: dict) -> list[dict]:
    """The lanes to classify — the ``lanes[]`` array, or a single synthesized lane
    from the top-level + session fields for single-lane (paper/demo) snapshots."""
    lanes = snap.get("lanes")
    if isinstance(lanes, list) and lanes:
        return lanes
    tm = snap.get("time_machine")
    if not tm:
        return []
    sess = snap.get("session") or {}
    tf = sess.get("timeframe") or next(iter((tm.get("health") or {}).keys()), "1h")
    return [{
        "lane_id": snap.get("lane_id") or snap.get("strategy_id"),
        "strategy_id": snap.get("strategy_id"),
        "symbol": snap.get("symbol"),
        "timeframe": tf,
        "mode": snap.get("mode"),
        "time_machine": tm,
        "latency": snap.get("latency"),
        "latency_recovery": sess.get("latency_recovery") or snap.get("latency_recovery"),
        "decision_skips": snap.get("decision_skips"),
        "arm_blocked": sess.get("arm_blocked") or snap.get("arm_blocked"),
        "drawdown_pct": sess.get("drawdown_pct"),
        "dd_limit_pct": sess.get("dd_limit_pct"),
        "trial_scorecard": sess.get("trial_scorecard"),
        "feed": (snap.get("feed_health") or {}).get("candles"),
    }]


def _dd_band(dd, lim) -> str:
    if dd is None:
        return "unknown"
    if lim is None:
        return "ok"
    if dd >= lim:
        return "blocked"
    if dd >= 0.8 * lim:
        return "degraded"
    return "ok"


def _verdict_tone(v) -> str:
    return {"PASS": "ok", "FAIL": "blocked", "PENDING": "degraded"}.get(v, "unknown")


def _latency_metric(
    latency: object, name: str, alias: str | None = None
) -> Mapping[str, object] | None:
    if not isinstance(latency, Mapping):
        return None
    raw = latency.get(name)
    if not isinstance(raw, Mapping) and alias:
        raw = latency.get(alias)
    return raw if isinstance(raw, Mapping) else None


def _latency_samples(latency: object) -> list[int]:
    if not isinstance(latency, Mapping):
        return []
    samples: list[int] = []
    for name in ("bar_close_processing_ms", "feed_lag_ms", "decision_lag_ms"):
        metric = latency.get(name)
        if isinstance(metric, Mapping):
            try:
                samples.append(int(metric.get("n") or 0))
            except (TypeError, ValueError):
                pass
    return samples


def lane_bands(lane: dict) -> dict:
    tf = str(lane.get("timeframe") or "")
    tm = lane.get("time_machine") or {}
    # Gate/colour on missed close delivery, not raw time since the last closed
    # candle.  Old snapshots fall back to age_ms until every process is on the
    # new schema.
    overdue = (tm.get("closed_bar_overdue_ms") or {}).get(tf)
    age = overdue if overdue is not None else (tm.get("age_ms") or {}).get(tf)
    lat = lane.get("latency") or {}
    decision_stats = _latency_metric(lat, "decision_lag_ms")
    bar_stats = _latency_metric(lat, "bar_close_processing_ms", "feed_lag_ms")
    decision_soft, decision_hard, decision_recovery = LT.decision_compute_limits(tf)
    dlag = LT.classify_latency_stats(
        decision_stats,
        soft_ms=decision_soft,
        hard_ms=decision_hard,
        recovery_ms=decision_recovery,
    )
    blag = LT.classify_latency_stats(
        bar_stats,
        soft_ms=LT.CLOSED_BAR_LAG_SOFT_P99_MS,
        hard_ms=LT.CLOSED_BAR_LAG_HARD_P99_MS,
        recovery_ms=LT.CLOSED_BAR_LAG_RECOVERY_MS,
    )
    sc = lane.get("trial_scorecard") or {}
    return {
        "age": _BAND[LT.classify_tm_age(tf, age)],
        "bar_close_lag": _BAND[blag],
        "decision_lag": _BAND[dlag],
        "dd": _dd_band(lane.get("drawdown_pct"), lane.get("dd_limit_pct")),
        "verdict_tone": _verdict_tone(sc.get("verdict")),
    }


def timeframe_health(lane: Mapping[str, object]) -> tuple[str, float | None]:
    """Return decision-timeframe transport state from the canonical snapshot."""
    timeframe = str(lane.get("timeframe") or "")
    machine = lane.get("time_machine")
    machine = machine if isinstance(machine, Mapping) else {}
    health = machine.get("health")
    health = health if isinstance(health, Mapping) else {}
    ages = machine.get("age_ms")
    ages = ages if isinstance(ages, Mapping) else {}
    feed_health = lane.get("feed_health")
    feed_health = feed_health if isinstance(feed_health, Mapping) else {}
    status = str(
        health.get(timeframe)
        or lane.get("feed")
        or feed_health.get("candles")
        or "unknown"
    ).lower()
    raw_age = ages.get(timeframe)
    if raw_age is None:
        raw_age = lane.get("staleness_ms") or feed_health.get("last_update_ms")
    try:
        age = round(float(str(raw_age)), 3) if raw_age is not None else None
    except (TypeError, ValueError):
        age = None
    if _blocks_candle_path(lane):
        status = "blocked"
    return status, age


def lane_health(lane: Mapping[str, object], *, has_problem: bool = False) -> str:
    """Canonical per-lane health used by every dashboard projection."""
    if lane.get("arm_blocked") or lane.get("gapped_candles"):
        return "blocked"
    if has_problem or lane.get("degraded"):
        return "degraded"

    feed_health = lane.get("feed_health")
    feed_health = feed_health if isinstance(feed_health, Mapping) else {}
    feed = str(lane.get("feed") or feed_health.get("candles") or "").lower()
    if feed and feed not in {"ok", "live"}:
        return "degraded" if "warm" in feed else "blocked"

    supplied = lane.get("bands")
    bands = supplied if isinstance(supplied, Mapping) else lane_bands(dict(lane))
    values = [
        str(bands.get(name) or "unknown")
        for name in ("age", "bar_close_lag", "decision_lag", "dd")
    ]
    if "blocked" in values:
        return "blocked"
    if "degraded" in values:
        return "degraded"
    sample_counts = _latency_samples(lane.get("latency"))
    if sample_counts and min(sample_counts) < LT.LATENCY_GATE_MIN_SAMPLES:
        return "unknown"
    if feed in {"ok", "live"} or "ok" in values:
        return "ok"
    return "unknown"


def compute_chips(snap: dict) -> dict:
    """The five safe-to-arm chips (SYSTEM/FEED/CANDLE/DECISION/RISK). UNKNOWN never
    fakes OK. SYSTEM is the kill-dominant rollup of the rest."""
    lanes = lane_rows(snap)
    # Measurement lanes cannot arm by design. When real SHADOW/PAPER lanes are
    # present, a measurement-only venue limitation (for example Delta lacking
    # a canonical tick candle) must remain visible on that lane without
    # declaring every scanner's CANDLE and DECISION path blocked. A genuinely
    # measurement-only fleet still classifies all lanes so missing data is not
    # hidden.
    arm_lanes = [
        lane
        for lane in lanes
        if str(lane.get("observation_class") or "").lower() != "measurement"
        and str(lane.get("strategy_id") or "").lower() != "measurement_only_v1"
    ]
    decision_lanes = arm_lanes or lanes
    kill = bool(snap.get("kill_switch_active"))

    candle, c_label = "unknown", "no telemetry"
    for lane in decision_lanes:
        tm = lane.get("time_machine") or {}
        health = tm.get("health")
        if not health:
            continue
        if candle == "unknown":
            candle, c_label = "ok", "ok"
        h = health.get(lane.get("timeframe"))
        if h and h != "ok":
            candle, c_label = _worse(candle, "blocked"), f"decision-TF {h}"
        if _blocks_candle_path(lane):      # candle-integrity arm blocks only
            candle, c_label = _worse(candle, "blocked"), "arms blocked"
        a1 = (tm.get("age_ms") or {}).get("1m")
        if a1 is not None and a1 > LT.TM_AGE_SOFT_P99_MS.get("1m", 1e18) and candle == "ok":
            candle, c_label = "degraded", "1m age soft"

    decision, d_label = "unknown", "no telemetry"
    # CURRENT block, not cumulative.
    skips = any(lane.get("arm_blocked") for lane in decision_lanes)
    decision_bands: list[str] = []
    bar_bands: list[str] = []
    sample_counts: list[int] = []
    for lane in decision_lanes:
        lat = lane.get("latency")
        bands = lane.get("bands") if isinstance(lane.get("bands"), Mapping) else lane_bands(lane)
        decision_bands.append(str(bands.get("decision_lag") or "unknown"))
        bar_bands.append(str(bands.get("bar_close_lag") or "unknown"))
        sample_counts.extend(_latency_samples(lat))
    if skips:
        decision, d_label = "blocked", "new arms blocked"
    elif "blocked" in bar_bands:
        decision, d_label = "blocked", "bar close lag"
    elif "blocked" in decision_bands:
        decision, d_label = "blocked", "compute lag"
    elif "unknown" in decision_bands + bar_bands and sample_counts:
        decision = "unknown"
        d_label = f"collecting {min(sample_counts)}/{LT.LATENCY_GATE_MIN_SAMPLES}"
    elif any(b != "unknown" for b in decision_bands + bar_bands):
        if "degraded" in bar_bands:
            decision, d_label = "degraded", "bar close lag"
        elif "degraded" in decision_bands:
            decision, d_label = "degraded", "compute lag"
        else:
            decision, d_label = "ok", "ok"
    elif sample_counts:
        decision = "unknown"
        d_label = f"collecting {max(sample_counts)}/{LT.LATENCY_GATE_MIN_SAMPLES}"

    feed, f_label = "unknown", "—"
    cand = str((snap.get("feed_health") or {}).get("candles") or "").lower()
    if cand:
        if "ok" in cand or "live" in cand:
            feed, f_label = "ok", "live"
        elif "warm" in cand:
            feed, f_label = "degraded", "warming"
        else:
            feed, f_label = "blocked", cand[:12]
    for lane in lanes:
        f = str(lane.get("feed") or "").lower()
        if f and "ok" not in f and "live" not in f:
            base = "ok" if feed == "unknown" else feed
            feed = _worse(base, "degraded" if "warm" in f else "blocked")

    risk, r_label = "ok", "ok"
    rs = str(snap.get("risk_status") or "ok").lower()
    streak = int(snap.get("consecutive_losses") or 0)
    if kill:
        risk, r_label = "blocked", "kill tripped"
    elif rs and rs != "ok":
        risk, r_label = "blocked", rs[:14]
    elif streak >= 3:
        risk, r_label = "degraded", f"{streak} loss streak"

    if kill:
        system, s_label = "blocked", "kill tripped"
    else:
        required = (candle, decision, feed, risk)
        if "blocked" in required:
            system, s_label = "blocked", "blocked"
        elif "degraded" in required:
            system, s_label = "degraded", "degraded"
        elif "unknown" in required:
            system, s_label = "unknown", "incomplete telemetry"
        else:
            system, s_label = "ok", "nominal"

    return {
        "SYSTEM": {"band": system, "label": s_label},
        "FEED": {"band": feed, "label": f_label},
        "CANDLE": {"band": candle, "label": c_label},
        "DECISION": {"band": decision, "label": d_label},
        "RISK": {"band": risk, "label": r_label},
    }


def annotate(snap: dict) -> dict:
    """Attach `chips` (top-level) + per-lane `bands` to an assembled snapshot dict,
    in place. Safe to call on both multi-lane and single-lane snapshots."""
    snap["chips"] = compute_chips(snap)
    lanes = snap.get("lanes")
    if isinstance(lanes, list):
        for lane in lanes:
            lane["bands"] = lane_bands(lane)
    else:
        # single-lane: expose the synthesized lane's bands top-level for the client
        rows = lane_rows(snap)
        if rows:
            snap["lane_bands"] = lane_bands(rows[0])
    return snap
