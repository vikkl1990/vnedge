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

from vnedge.runtime import latency_thresholds as LT

# arm-gate bands -> UI bands (ok/degraded/blocked/unknown)
_BAND = {"ok": "ok", "soft": "degraded", "hard": "blocked", "unknown": "unknown"}
_RANK = {"blocked": 3, "degraded": 2, "ok": 1, "unknown": 0}


def _worse(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def _skip_count(o) -> int:
    return sum(int(v) for v in o.values()) if isinstance(o, dict) else 0


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
        "decision_skips": snap.get("decision_skips"),
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


def lane_bands(lane: dict) -> dict:
    tf = lane.get("timeframe")
    tm = lane.get("time_machine") or {}
    age = (tm.get("age_ms") or {}).get(tf)
    lat = lane.get("latency") or {}
    p95 = (lat.get("decision_lag_ms") or {}).get("p95") if isinstance(lat, dict) else None
    dlag = LT.classify_p99(p95, LT.DECISION_COMPUTE_SOFT_P99_MS, LT.DECISION_COMPUTE_HARD_P99_MS)
    sc = lane.get("trial_scorecard") or {}
    return {
        "age": _BAND[LT.classify_tm_age(tf, age)],
        "decision_lag": _BAND[dlag],
        "dd": _dd_band(lane.get("drawdown_pct"), lane.get("dd_limit_pct")),
        "verdict_tone": _verdict_tone(sc.get("verdict")),
    }


def compute_chips(snap: dict) -> dict:
    """The five safe-to-arm chips (SYSTEM/FEED/CANDLE/DECISION/RISK). UNKNOWN never
    fakes OK. SYSTEM is the kill-dominant rollup of the rest."""
    lanes = lane_rows(snap)
    kill = bool(snap.get("kill_switch_active"))

    candle, c_label = "unknown", "no telemetry"
    for l in lanes:
        tm = l.get("time_machine") or {}
        health = tm.get("health")
        if not health:
            continue
        if candle == "unknown":
            candle, c_label = "ok", "ok"
        h = health.get(l.get("timeframe"))
        if h and h != "ok":
            candle, c_label = _worse(candle, "blocked"), f"decision-TF {h}"
        if _skip_count(l.get("decision_skips")) > 0:
            candle, c_label = _worse(candle, "blocked"), "arms blocked"
        a1 = (tm.get("age_ms") or {}).get("1m")
        if a1 is not None and a1 > LT.TM_AGE_SOFT_P99_MS.get("1m", 1e18) and candle == "ok":
            candle, c_label = "degraded", "1m age soft"

    decision, d_label = "unknown", "no telemetry"
    skips = any(_skip_count(l.get("decision_skips")) > 0 for l in lanes)
    lat_vals = [(l.get("latency") or {}).get("decision_lag_ms", {}).get("p95")
                for l in lanes if isinstance(l.get("latency"), dict)]
    lat_vals = [v for v in lat_vals if isinstance(v, (int, float))]
    if skips:
        decision, d_label = "blocked", "new arms blocked"
    elif lat_vals:
        soft = any(v > LT.DECISION_COMPUTE_SOFT_P99_MS for v in lat_vals)
        decision, d_label = ("degraded", "compute lag") if soft else ("ok", "ok")

    feed, f_label = "unknown", "—"
    cand = str((snap.get("feed_health") or {}).get("candles") or "").lower()
    if cand:
        if "ok" in cand or "live" in cand:
            feed, f_label = "ok", "live"
        elif "warm" in cand:
            feed, f_label = "degraded", "warming"
        else:
            feed, f_label = "blocked", cand[:12]
    for l in lanes:
        f = str(l.get("feed") or "").lower()
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
        system = "ok"
        for x in (candle, decision, feed, risk):
            if x != "unknown":
                system = _worse(system, x)
        s_label = {"ok": "nominal", "degraded": "degraded", "blocked": "blocked"}[system]

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
        for l in lanes:
            l["bands"] = lane_bands(l)
    else:
        # single-lane: expose the synthesized lane's bands top-level for the client
        rows = lane_rows(snap)
        if rows:
            snap["lane_bands"] = lane_bands(rows[0])
    return snap
