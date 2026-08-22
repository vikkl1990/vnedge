"""Read-only projections for the correction cockpit.

The dashboard displays runtime policy; it does not create policy.  These
helpers deliberately derive small, stable API payloads from the coalesced
snapshot and the strategy registry.  They cannot emit signals, intents,
orders, or promotion decisions.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from vnedge.dashboard.health_bands import lane_health, timeframe_health
from vnedge.runtime.latency_thresholds import LATENCY_GATE_MIN_SAMPLES
from vnedge.strategy.strategy_registry import (
    KILLED,
    RESEARCH_ONLY,
    is_capital_eligible,
    is_shadow_observe_eligible,
)

LIVE_BLOCKED_MESSAGE = (
    "Live entrypoint disabled — venue private stream / checklist incomplete. "
    "Paper/shadow only."
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    lanes = snapshot.get("lanes")
    if isinstance(lanes, list):
        return [lane for lane in lanes if isinstance(lane, Mapping)]
    return [snapshot]


def _mode(raw: object, strategy_id: str, *, killed: bool) -> str:
    if killed:
        return "off"
    text = str(raw or "").lower()
    if "shadow" in text and is_shadow_observe_eligible(strategy_id):
        return "shadow"
    if strategy_id in RESEARCH_ONLY:
        return "measurement"
    if "paper" in text:
        return "paper"
    if "shadow" in text:
        return "shadow"
    if "measurement" in text:
        return "measurement"
    if "warming" in text:
        return "measurement" if strategy_id in RESEARCH_ONLY else "off"
    return "off"


def _eligibility(strategy_id: str) -> str:
    if strategy_id in KILLED:
        return "KILLED"
    if strategy_id in RESEARCH_ONLY:
        return "RESEARCH_ONLY"
    if is_capital_eligible(strategy_id):
        return "eligible"
    return "unknown"


def _age_seconds(value: object, now: datetime) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return round(max(0.0, (now - parsed.astimezone(UTC)).total_seconds()), 1)


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return round(number, 3)


def _latency_value(lane: Mapping[str, Any], name: str) -> float | None:
    latency = _mapping(lane.get("latency"))
    metric = _mapping(latency.get(name))
    return _number(metric.get("p95") if metric else latency.get(name))


def _latency_value_with_alias(
    lane: Mapping[str, Any], name: str, alias: str
) -> float | None:
    value = _latency_value(lane, name)
    return value if value is not None else _latency_value(lane, alias)


def _latency_samples(lane: Mapping[str, Any], name: str, alias: str | None = None) -> int:
    latency = _mapping(lane.get("latency"))
    metric = _mapping(latency.get(name))
    if not metric and alias:
        metric = _mapping(latency.get(alias))
    try:
        return int(metric.get("n") or 0)
    except (TypeError, ValueError):
        return 0


def _position_count(lane: Mapping[str, Any]) -> int:
    positions = lane.get("positions")
    if isinstance(positions, list):
        return len(positions)
    try:
        return max(0, int(positions or 0))
    except (TypeError, ValueError):
        return 0


def _skip_count(lane: Mapping[str, Any]) -> int:
    skips = _mapping(lane.get("decision_skips"))
    return sum(int(value or 0) for value in skips.values())


def _last_signal_reason(
    lane: Mapping[str, Any], *, eligibility: str, mode: str
) -> str:
    if eligibility == "KILLED":
        return "strategy_killed"
    if mode == "measurement":
        return "observe_only"
    blocked = lane.get("arm_blocked")
    if blocked:
        if isinstance(blocked, Mapping):
            return str(blocked.get("reason") or blocked.get("detail") or "arm_blocked")
        return str(blocked)
    evaluation = _mapping(lane.get("last_eval"))
    return str(
        evaluation.get("reason")
        or evaluation.get("signal_reason")
        or lane.get("last_risk_reject")
        or "no_signal_observed"
    )


def _current_waiting_reason(lane: Mapping[str, Any], fallback: str) -> str:
    """Current operational reason, distinct from historical last rejection."""
    blocked = lane.get("arm_blocked")
    if blocked:
        if isinstance(blocked, Mapping):
            return str(blocked.get("reason") or blocked.get("detail") or "arm_blocked")
        return str(blocked)
    recovery = _mapping(lane.get("latency_recovery"))
    states = [
        _mapping(value)
        for value in recovery.values()
        if isinstance(value, Mapping)
    ]
    recovering = next(
        (state for state in states if state.get("state") == "recovering"), None
    )
    if recovering:
        return (
            "latency_recovering_"
            f"{int(recovering.get('healthy_samples') or 0)}/"
            f"{int(recovering.get('required_samples') or 0)}"
        )
    if any(state.get("state") == "recovered" for state in states):
        return "latency_recovered_p95_cooling"
    return fallback


def build_lanes_payload(
    snapshot: Mapping[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Project the active roster without allowing mode to imply permission."""
    at = now or datetime.now(UTC)
    runtime = _mapping(snapshot.get("runtime_control"))
    problem_rows = _mapping(snapshot.get("lane_health")).get("problems")
    problems = {
        str(item.get("lane_id")): str(item.get("verdict") or "degraded")
        for item in (problem_rows if isinstance(problem_rows, list) else [])
        if isinstance(item, Mapping) and item.get("lane_id")
    }
    result: list[dict[str, Any]] = []
    for lane in _rows(snapshot):
        strategy_id = str(lane.get("strategy_id") or "")
        eligibility = _eligibility(strategy_id)
        mode = _mode(
            lane.get("mode"), strategy_id, killed=eligibility == "KILLED"
        )
        observation_class = (
            "shadow_observe"
            if mode == "shadow" and is_shadow_observe_eligible(strategy_id)
            else "measurement"
            if mode == "measurement"
            else None
        )
        capital = (
            mode == "paper"
            and eligibility == "eligible"
            and bool(runtime.get("orders_allowed"))
        )
        candle_status, candle_age_ms = timeframe_health(lane)
        plan = _mapping(lane.get("plan_overlay"))
        sizing = _mapping(lane.get("sizing_profile"))
        lane_id = str(lane.get("lane_id") or "")
        health = lane_health(lane, has_problem=lane_id in problems)
        reason = _last_signal_reason(lane, eligibility=eligibility, mode=mode)
        waiting_reason = _current_waiting_reason(lane, reason)
        result.append(
            {
                "lane_id": str(lane.get("lane_id") or strategy_id or "unknown"),
                "strategy_id": strategy_id or "unknown",
                "eligibility": eligibility,
                "mode": mode,
                "observation_class": observation_class,
                "exchange": str(lane.get("exchange") or lane.get("lane_exchange") or ""),
                "symbol": str(lane.get("symbol") or ""),
                "timeframe": str(lane.get("timeframe") or ""),
                "capital": capital,
                "venue_rtt_ms": _number(lane.get("venue_rtt_ms"))
                or _latency_value(lane, "venue_rtt_ms"),
                "candle_status": candle_status,
                "candle_age_ms": candle_age_ms,
                "bar_close_processing_ms": _latency_value_with_alias(
                    lane, "bar_close_processing_ms", "feed_lag_ms"
                ),
                "decision_lag_ms": _latency_value(lane, "decision_lag_ms"),
                "latency_samples": {
                    "bar_close": _latency_samples(
                        lane, "bar_close_processing_ms", "feed_lag_ms"
                    ),
                    "decision": _latency_samples(lane, "decision_lag_ms"),
                    "required": LATENCY_GATE_MIN_SAMPLES,
                },
                "latency_recovery": dict(_mapping(lane.get("latency_recovery"))),
                "arm_skips": _skip_count(lane),
                "last_signal_age_seconds": (
                    None
                    if mode == "measurement"
                    else _age_seconds(lane.get("last_fired_ts"), at)
                ),
                "last_signal_reason": reason,
                "current_waiting_reason": waiting_reason,
                "cost_profile": str(lane.get("cost_profile") or "unreported"),
                "round_trip_bps": _number(
                    plan.get("round_trip_bps") or lane.get("round_trip_bps")
                ),
                "health": health,
                "health_reason": problems.get(str(lane.get("lane_id") or "")),
                "shadow_perf": lane.get("shadow_perf") if mode == "shadow" else None,
                "equity_usd": _number(lane.get("equity")),
                "realized_pnl_usd": _number(lane.get("realized_pnl")),
                "unrealized_pnl_usd": _number(lane.get("unrealized_pnl")),
                "open_positions": _position_count(lane),
                "funnel": dict(_mapping(lane.get("funnel"))),
                # Measurement lanes have a nominal tracker balance because the
                # shared runtime owns one, but that is not an actionable purse
                # or sizing contract.  Exposing it here made the cockpit imply
                # that every measurement row could size a trade.  Only paper
                # and explicit shadow-observe lanes may publish sizing truth.
                "sizing_profile": (
                    dict(sizing)
                    if sizing and (mode == "paper" or observation_class == "shadow_observe")
                    else None
                ),
                "active_plan": (
                    dict(_mapping(lane.get("active_plan")))
                    if lane.get("active_plan")
                    else None
                ),
                "last_eval": (
                    dict(_mapping(lane.get("last_eval")))
                    if lane.get("last_eval")
                    else None
                ),
                "last_reject_reason": (
                    str(lane.get("last_reject_reason"))
                    if lane.get("last_reject_reason")
                    else None
                ),
                "why_no_fire": (
                    "measurement lane emits no OrderIntent by design"
                    if mode == "measurement"
                    else "strategy is killed and forced off"
                    if eligibility == "KILLED"
                    else waiting_reason
                ),
            }
        )

    capital_count = sum(bool(row["capital"]) for row in result)
    observe_count = sum(
        row["observation_class"] == "shadow_observe" for row in result
    )
    shadow_rows = [
        row for row in result if row["observation_class"] == "shadow_observe"
    ]
    measurement_rows = [
        row for row in result if row["observation_class"] == "measurement"
    ]
    paper_rows = [row for row in result if row["mode"] == "paper"]

    def purse(rows: list[dict[str, Any]]) -> float:
        total = 0.0
        for row in rows:
            sizing = _mapping(row.get("sizing_profile"))
            value = sizing.get("starting_equity_usd")
            if value is None:
                value = row.get("equity_usd")
            try:
                total += float(value or 0.0)
            except (TypeError, ValueError):
                continue
        return round(total, 2)

    shared_purse = runtime.get("shadow_shared_purse_usd")
    portfolio = {
        "shadow_purse_usd": round(float(shared_purse), 2)
        if shared_purse is not None else purse(shadow_rows),
        "paper_purse_usd": purse(paper_rows),
        "measurement_nominal_usd": purse(measurement_rows),
        "shadow_lane_count": len(shadow_rows),
        "paper_lane_count": len(paper_rows),
        "measurement_lane_count": len(measurement_rows),
        "shadow_open_positions": sum(row["open_positions"] for row in shadow_rows),
        "shadow_pending_intents": sum(
            int(_mapping(row.get("shadow_perf")).get("pending_shadow_intents") or 0)
            for row in shadow_rows
        ),
    }
    observer_strategies = runtime.get("shadow_observe_strategies")
    observer_timeframes = runtime.get("shadow_observe_timeframes")
    return {
        "lanes": result,
        "capital_roster_size": capital_count,
        "measurement_only": capital_count == 0,
        "banner": (
            "SHADOW_OBSERVE · virtual only — no capital strategies."
            if capital_count == 0 and observe_count
            else "No capital strategies — measurement only."
            if capital_count == 0
            else None
        ),
        "shadow_observe_lanes": observe_count,
        "shadow_observe_strategies": [
            str(value)
            for value in (
                observer_strategies if isinstance(observer_strategies, list) else []
            )
        ],
        "shadow_observe_timeframes": [
            str(value)
            for value in (
                observer_timeframes if isinstance(observer_timeframes, list) else []
            )
        ],
        "lane_set_hash": str(runtime.get("lane_set_hash") or "") or None,
        "portfolio": portfolio,
        "read_only": True,
        "can_promote": False,
        "can_trade": False,
    }


def _feed_status(lanes: list[Mapping[str, Any]]) -> tuple[str, str]:
    feeds = [
        str(lane.get("feed") or _mapping(lane.get("feed_health")).get("candles") or "")
        .strip()
        .lower()
        for lane in lanes
    ]
    degraded = any(lane.get("degraded") or lane.get("gapped_candles") for lane in lanes)
    if degraded or any("gap" in feed or "error" in feed for feed in feeds):
        return "gap", "gap / degraded"
    if any(feed and feed not in {"ok", "live"} for feed in feeds):
        return "stale", "stale / warming"
    if feeds and all(feed in {"ok", "live"} for feed in feeds):
        return "healthy", "healthy"
    return "unknown", "no telemetry"


def _journal(snapshot: Mapping[str, Any], lanes: list[Mapping[str, Any]]) -> dict[str, Any]:
    journals = [_mapping(snapshot.get("journal"))]
    journals.extend(_mapping(lane.get("journal")) for lane in lanes)
    degraded = any(bool(journal.get("recovery_degraded")) for journal in journals)
    unavailable = str(snapshot.get("last_journal_write") or "").lower() == "unavailable"
    unavailable = unavailable or any(journal.get("available") is False for journal in journals)
    quarantine = next(
        (str(journal.get("quarantine_path")) for journal in journals if journal.get("quarantine_path")),
        None,
    )
    recovery_error = next(
        (str(journal.get("recovery_error")) for journal in journals if journal.get("recovery_error")),
        None,
    )
    return {
        "available": not unavailable,
        "recovery_degraded": degraded,
        "quarantine_path": quarantine,
        "recovery_error": recovery_error,
        "entries_blocked": unavailable or degraded,
    }


def _daily_halt(snapshot: Mapping[str, Any], lanes: list[Mapping[str, Any]]) -> dict[str, Any]:
    daily_pnl = float(snapshot.get("daily_pnl") or 0.0)
    peak = float(snapshot.get("peak_equity") or 0.0)
    limit: float | None = None
    for lane in lanes:
        scorecard = _mapping(lane.get("trial_scorecard"))
        criteria = scorecard.get("criteria")
        for criterion in criteria if isinstance(criteria, list) else []:
            if isinstance(criterion, Mapping) and criterion.get("name") == "daily_loss":
                threshold = criterion.get("threshold")
                try:
                    limit = abs(float(threshold)) if threshold is not None else None
                except (TypeError, ValueError):
                    limit = None
                break
        if limit is not None:
            break
    used = max(0.0, -daily_pnl)
    return {
        "used_usd": round(used, 2),
        "limit_usd": round(limit, 2) if limit is not None else None,
        "used_pct_of_peak_equity": round(100.0 * used / peak, 3) if peak > 0 else None,
        "active": bool(limit is not None and used >= limit),
    }


def _gateway(snapshot: Mapping[str, Any], lanes: list[Mapping[str, Any]]) -> dict[str, Any]:
    reasons: list[str] = []
    for row in [snapshot, *lanes]:
        value = row.get("last_risk_reject")
        if value:
            reasons.append(str(value))
    counts = Counter(reasons)
    return {
        "last_reject_reasons": [
            {"reason": reason, "count": count} for reason, count in counts.most_common(10)
        ],
        "observed_reject_count": len(reasons),
        "window": "current_snapshot",
    }


def _reconciliation(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    raw = _mapping(
        snapshot.get("reconciliation")
        or snapshot.get("recon")
        or _mapping(snapshot.get("runtime_control")).get("reconciliation")
    )
    last_success = raw.get("last_success_at") or raw.get("last_success_ts")
    return {
        "status": str(raw.get("status") or "not_reported"),
        "last_success_at": str(last_success) if last_success else None,
        "last_success_age_seconds": _number(raw.get("last_success_age_seconds")),
        "fail_count": int(raw.get("fail_count") or raw.get("failures") or 0),
        "clean": bool(raw.get("clean")) if "clean" in raw else False,
    }


def _live_checklist(
    snapshot: Mapping[str, Any], *, journal: Mapping[str, Any], recon: Mapping[str, Any]
) -> list[dict[str, Any]]:
    runtime = _mapping(snapshot.get("runtime_control"))
    live = _mapping(snapshot.get("live_gates"))
    return [
        {"id": "kill_clear", "label": "kill clear", "ok": not bool(snapshot.get("kill_switch_active"))},
        {"id": "risk_frozen", "label": "risk frozen", "ok": bool(runtime.get("risk_frozen") or live.get("risk_frozen"))},
        {"id": "recon_clean", "label": "recon clean", "ok": bool(recon.get("clean"))},
        {"id": "journal_writable", "label": "journal writable", "ok": bool(journal.get("available")) and not bool(journal.get("entries_blocked"))},
        {"id": "live_flags", "label": "three live flags", "ok": bool(live.get("three_live_flags"))},
        {"id": "trade_keys", "label": "trade-only keys", "ok": bool(live.get("trade_keys"))},
        {"id": "ladder", "label": "ladder attestation", "ok": bool(live.get("ladder_attestation"))},
    ]


def build_risk_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Build the C1/C3 posture contract entirely from read-only state."""
    lanes = _rows(snapshot)
    runtime = _mapping(snapshot.get("runtime_control"))
    feed_status, feed_label = _feed_status(lanes)
    journal = _journal(snapshot, lanes)
    recon = _reconciliation(snapshot)
    checklist = _live_checklist(snapshot, journal=journal, recon=recon)
    lane_projection = build_lanes_payload(snapshot)
    projected_lanes = lane_projection["lanes"]
    portfolio = lane_projection["portfolio"]
    capital_size = int(runtime.get("capital_roster_size") or 0)
    live_enabled = bool(snapshot.get("live_trading_enabled"))
    if live_enabled:
        runtime_mode = str(snapshot.get("mode") or "live_blocked").split()[0]
    elif capital_size > 0:
        runtime_mode = "paper"
    elif int(portfolio["shadow_lane_count"]) > 0:
        # SHADOW_OBSERVE is intentionally RESEARCH_ONLY, but it is still an
        # active virtual decision path.  Calling the fleet "measurement" here
        # hid the very scanners/operators were trying to verify.
        runtime_mode = "shadow"
    elif any("shadow" in str(lane.get("mode") or "").lower() for lane in lanes):
        runtime_mode = "measurement"
    else:
        runtime_mode = "measurement"

    streams: list[dict[str, str]] = []
    seen: set[str] = set()
    for lane in lanes:
        exchange = str(lane.get("exchange") or lane.get("lane_exchange") or "unknown")
        if exchange in seen:
            continue
        seen.add(exchange)
        public = str(lane.get("feed") or _mapping(lane.get("feed_health")).get("candles") or "unknown")
        streams.append(
            {
                "exchange": exchange,
                "public_feed": public,
                "private_stream": (
                    "not_implemented" if exchange == "delta_india" else "not_required"
                ),
            }
        )

    return {
        "runtime_mode": runtime_mode,
        "runtime_label": str(snapshot.get("mode") or "unknown"),
        "capital": {"enabled": capital_size > 0, "roster_size": capital_size},
        "build_sha": str(snapshot.get("build_sha") or "dev"),
        "kill": {
            "active": bool(snapshot.get("kill_switch_active")),
            "latched": bool(snapshot.get("kill_switch_active")),
        },
        "feed": {"status": feed_status, "label": feed_label},
        "live": {
            "blocked": not live_enabled,
            "message": LIVE_BLOCKED_MESSAGE if not live_enabled else None,
            "delta_private_status": "not_implemented",
        },
        "daily_halt": _daily_halt(snapshot, lanes),
        "journal": journal,
        "gateway": _gateway(snapshot, lanes),
        "positions": {
            "shadow_open": int(portfolio["shadow_open_positions"]),
            "shadow_pending_intents": int(portfolio["shadow_pending_intents"]),
            "unresolved_orders": sum(
                1
                for row in lanes
                for order in (row.get("open_orders") or [])
                if isinstance(order, Mapping)
                and str(order.get("state") or "").lower()
                in {"timeout_unknown", "unresolved", "pending_unknown"}
            ),
        },
        "portfolio": portfolio,
        "sizing_profiles": [
            {
                "lane_id": row["lane_id"],
                "symbol": row["symbol"],
                **dict(_mapping(row.get("sizing_profile"))),
            }
            for row in projected_lanes
            if row.get("sizing_profile")
        ],
        "breaker": {
            "loss_streak": int(snapshot.get("consecutive_losses") or 0),
            "active": int(snapshot.get("consecutive_losses") or 0) >= 3,
            "threshold": 3,
        },
        "reconciliation": recon,
        "live_checklist": {
            "passed": sum(bool(item["ok"]) for item in checklist),
            "total": len(checklist),
            "items": checklist,
        },
        "streams": streams,
        "read_only": True,
        "can_trade": False,
    }
