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

from vnedge.strategy.strategy_registry import (
    KILLED,
    RESEARCH_ONLY,
    is_capital_eligible,
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


def _lane_health(lane: Mapping[str, Any], problems: Mapping[str, str]) -> str:
    lane_id = str(lane.get("lane_id") or "")
    if lane_id in problems:
        return "degraded"
    if lane.get("degraded") or lane.get("arm_blocked"):
        return "degraded"
    feed = str(lane.get("feed") or _mapping(lane.get("feed_health")).get("candles") or "")
    if not feed:
        return "unknown"
    return "ok" if feed.lower() in {"ok", "live"} else "degraded"


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
        capital = (
            mode == "paper"
            and eligibility == "eligible"
            and bool(runtime.get("orders_allowed"))
        )
        result.append(
            {
                "lane_id": str(lane.get("lane_id") or strategy_id or "unknown"),
                "strategy_id": strategy_id or "unknown",
                "eligibility": eligibility,
                "mode": mode,
                "exchange": str(lane.get("exchange") or lane.get("lane_exchange") or ""),
                "symbol": str(lane.get("symbol") or ""),
                "timeframe": str(lane.get("timeframe") or ""),
                "capital": capital,
                "last_signal_age_seconds": (
                    None
                    if eligibility == "RESEARCH_ONLY"
                    else _age_seconds(lane.get("last_fired_ts"), at)
                ),
                "health": _lane_health(lane, problems),
            }
        )

    capital_count = sum(bool(row["capital"]) for row in result)
    return {
        "lanes": result,
        "capital_roster_size": capital_count,
        "measurement_only": capital_count == 0,
        "banner": (
            "No capital strategies — measurement only." if capital_count == 0 else None
        ),
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
        ]
    }


def build_risk_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Build the C1/C3 posture contract entirely from read-only state."""
    lanes = _rows(snapshot)
    runtime = _mapping(snapshot.get("runtime_control"))
    feed_status, feed_label = _feed_status(lanes)
    capital_size = int(runtime.get("capital_roster_size") or 0)
    live_enabled = bool(snapshot.get("live_trading_enabled"))
    if live_enabled:
        runtime_mode = str(snapshot.get("mode") or "live_blocked").split()[0]
    elif capital_size > 0:
        runtime_mode = "paper"
    elif any("shadow" in str(lane.get("mode") or "").lower() for lane in lanes):
        active_strategies = [
            str(lane.get("strategy_id") or "")
            for lane in lanes
            if str(lane.get("strategy_id") or "") not in KILLED
        ]
        runtime_mode = "measurement" if active_strategies and all(
            strategy_id in RESEARCH_ONLY for strategy_id in active_strategies
        ) else "shadow"
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
        "journal": _journal(snapshot, lanes),
        "gateway": _gateway(snapshot, lanes),
        "streams": streams,
        "read_only": True,
        "can_trade": False,
    }
