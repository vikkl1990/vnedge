"""Authoritative, read-only Pattern Atlas projection.

The Atlas is an operator diagnostic, not a second strategy registry.  Pattern
anatomy lives here on the server and is joined to the same lane snapshot and
evidence catalogue used by the rest of the dashboard.  The projection keeps
three truths separate:

* operations -- can the lane consume trustworthy data right now?
* setup -- did the frozen scanner contract find/accept a setup?
* evidence -- what has the exact strategy id proved outside this runtime?

Keeping those states separate prevents a healthy no-setup lane from looking
broken and prevents a green process badge from looking like validated edge.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from vnedge.research.scanner_catalog import EVIDENCE_AUTHORITY


@dataclass(frozen=True, slots=True)
class PatternDefinition:
    id: str
    name: str
    thesis: str
    family: Literal["expansion", "continuation", "reclaim", "reversal"]
    decision_tf: Literal["5m", "15m", "1h"]
    context: str
    entry_clock: str
    protection_clock: str
    regime: str
    direction: str
    rules: tuple[str, ...]
    invalidation: str
    economics: str
    caution: str
    strategy_ids: tuple[str, ...]
    sketch: str


PATTERN_DEFINITIONS: tuple[PatternDefinition, ...] = (
    PatternDefinition(
        id="squeeze-expansion",
        name="Compression → Expansion",
        thesis="A quiet 5m box releases with enough volume and executable quote acceptance.",
        family="expansion",
        decision_tf="5m",
        context="closed 5m box",
        entry_clock="BBO hold after close",
        protection_clock="ticks · 30–60m horizon",
        regime="range or trend pullback",
        direction="two-sided",
        rules=(
            "Compression is present before the break",
            "Closed bar clears the frozen box",
            "BBO holds beyond the level; spread and chase remain valid",
        ),
        invalidation="Back inside the box, stale/overflowed book, or the opposite box edge.",
        economics="Delta scalp tariff. A small gross move is not a setup that survived costs.",
        caution="Fastest family and the most fee-sensitive. Current evidence has been weak after booked costs.",
        strategy_ids=(
            "squeeze_expansion_breakout_v3",
            "squeeze_expansion_breakout_v4",
            "tick_accepted_breakout_v1",
        ),
        sketch="squeeze",
    ),
    PatternDefinition(
        id="range-expansion",
        name="Range Expansion",
        thesis="A prior 15m balance resolves beyond its boundary without using the forming candle.",
        family="expansion",
        decision_tf="15m",
        context="prior closed range",
        entry_clock="next open or BBO hold",
        protection_clock="ticks · 12h backstop",
        regime="range ending or expansion beginning",
        direction="two-sided",
        rules=(
            "A measurable range exists before the decision bar",
            "Expansion clears the prior boundary",
            "Room after costs remains before the next structure wall",
        ),
        invalidation="Return through the broken boundary or failed acceptance.",
        economics="Delta swing profile; gate reserve is not booked P&L.",
        caution="A large candle that already travelled through the target is not a fresh entry.",
        strategy_ids=(
            "range_expansion_observer_v3",
            "range_expansion_observer_v4",
            "range_expansion_realtime_v1",
            "range_expansion_realtime_v2",
        ),
        sketch="range",
    ),
    PatternDefinition(
        id="structure-bos",
        name="Break of Structure",
        thesis="Confirmed swings establish structure; a closed 15m break must agree with last-closed HTF context.",
        family="continuation",
        decision_tf="15m",
        context="confirmed swings · closed 4h",
        entry_clock="next open or BBO hold",
        protection_clock="ticks · structure invalidation",
        regime="continuation only",
        direction="with HTF",
        rules=(
            "Swing anchors are confirmed causally",
            "Break clears the latest structure with its frozen buffer",
            "4h context does not oppose the side",
        ),
        invalidation="Opposite confirmed swing or HTF bias invalidation.",
        economics="Swing tariff; stops are snapped to the Delta tick grid before sizing.",
        caution="Fractal confirmation is intentionally selective. More fires is not automatically better structure.",
        strategy_ids=(
            "structure_bos_1h",
            "structure_bos_15m_trigger_v2",
            "structure_bos_15m_trigger_v3",
            "structure_bos_realtime_v1",
            "structure_bos_realtime_v2",
        ),
        sketch="bos",
    ),
    PatternDefinition(
        id="htf-regime-continuation",
        name="HTF Regime Continuation",
        thesis="Weekly/daily/4h permission chooses the playbook; a 15m reclaim supplies the entry geometry.",
        family="continuation",
        decision_tf="15m",
        context="closed 1w · 1d · 4h",
        entry_clock="next 15m open",
        protection_clock="ticks · flatten on HTF invalidation",
        regime="continuation with one allowed side",
        direction="with HTF",
        rules=(
            "Last-closed weekly structure resolves up or down",
            "Daily EMA/MACD impulse is not fading at an extreme",
            "4h agrees and 15m reclaims in the permitted direction",
        ),
        invalidation="Closed 4h flips against the weekly side or the 15m reclaim fails.",
        economics="Delta swing profile; funding is applied only if a held position crosses a real print.",
        caution="V1 requires complete trade-lake weekly VWAP. V2 uses OHLC range/structure and is a separate frozen hypothesis.",
        strategy_ids=(
            "htf_regime_continuation_15m_v1",
            "htf_regime_continuation_15m_v2",
            "htf_structure_continuation_realtime_v1",
        ),
        sketch="regime",
    ),
    PatternDefinition(
        id="avwap-reclaim",
        name="Anchored VWAP Reclaim",
        thesis="Price regains an event-anchored cost basis after a causal swing anchor is confirmed.",
        family="reclaim",
        decision_tf="15m",
        context="dual AVWAP · confirmed swings",
        entry_clock="next 15m open",
        protection_clock="ticks · anchor failure",
        regime="pullback inside aligned structure",
        direction="with HTF",
        rules=(
            "Both anchors are causal and available",
            "Close reclaims the relevant AVWAP",
            "The opposite AVWAP does not create a strong conflict",
        ),
        invalidation="Loss of the reclaimed AVWAP plus structure failure.",
        economics="Swing tariff; AVWAP is context, never a fee bypass.",
        caution="A reclaim is location, not proof of expectancy. Thin samples remain under-sampled.",
        strategy_ids=("avwap_reclaim_15m_v1",),
        sketch="reclaim",
    ),
    PatternDefinition(
        id="session-continuation",
        name="Session Continuation",
        thesis="An active UTC block extends an already-aligned move after range and volume wake up.",
        family="continuation",
        decision_tf="15m",
        context="session clock · 4h bias",
        entry_clock="next open or BBO hold",
        protection_clock="ticks · session/structure exit",
        regime="continuation during eligible hours",
        direction="with HTF",
        rules=(
            "Evaluation is inside the frozen session",
            "Range/volume expansion clears its hour-of-day baseline",
            "The side agrees with higher-timeframe permission",
        ),
        invalidation="Session drive fails back through its origin or HTF permission disappears.",
        economics="Swing tariff. The busy hour is not itself directional edge.",
        caution="Outside-session rows are correct no-trades, not a dead scanner.",
        strategy_ids=(
            "session_continuation_15m_v1",
            "session_continuation_realtime_v1",
            "session_continuation_realtime_v2",
        ),
        sketch="session",
    ),
    PatternDefinition(
        id="liquidity-sweep",
        name="Liquidity Sweep Reversal",
        thesis="A closed 15m bar trades beyond a prior extreme and rejects back through it.",
        family="reversal",
        decision_tf="15m",
        context="prior swing extreme",
        entry_clock="next 15m open",
        protection_clock="ticks · sweep extreme",
        regime="mean reversion only",
        direction="counter-move",
        rules=(
            "A real prior liquidity extreme exists",
            "The decision bar sweeps and closes back inside",
            "Counter-trend permission is explicit; continuation regime blocks the fade",
        ),
        invalidation="Price accepts beyond the swept extreme.",
        economics="Swing tariff. Gross-negative evidence cannot be repaired with optimistic fees.",
        caution="This family has produced negative local replay evidence and must remain research-only.",
        strategy_ids=("liquidity_sweep_reversal_15m_v1",),
        sketch="sweep",
    ),
    PatternDefinition(
        id="trend-pullback",
        name="Trend Pullback",
        thesis="A 1h pullback preserves the larger trend and resumes without chasing the impulse bar.",
        family="reclaim",
        decision_tf="1h",
        context="closed 4h/daily direction",
        entry_clock="next 1h open",
        protection_clock="ticks · 48h backstop",
        regime="continuation after pullback",
        direction="with HTF",
        rules=(
            "Higher-timeframe trend remains intact",
            "Pullback reaches a defined value/structure zone",
            "Closed 1h bar resumes in the permitted direction",
        ),
        invalidation="Pullback becomes an HTF structure break.",
        economics="Swing tariff; larger horizon is intended to amortize fixed round-trip cost.",
        caution="One positive trade is a case study, not a scorecard.",
        strategy_ids=("trend_pullback_1h_v1", "trend_squeeze_continuation_1h_v1"),
        sketch="pullback",
    ),
)


_OPS_RANK = {"unknown": 0, "ok": 1, "degraded": 2, "blocked": 3}
_SETUP_RANK = {
    "not_rostered": 0,
    "watching": 1,
    "session_blocked": 2,
    "degraded": 3,
    "armed": 4,
    "holding": 5,
    "accepted": 6,
}


def _unique(values: Iterable[Any], *, limit: int = 12) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _failed_gates(lane: dict[str, Any]) -> list[str]:
    last_eval = lane.get("last_eval") or {}
    gates = last_eval.get("all_failed_gates") or last_eval.get("failed_gates") or []
    if isinstance(gates, str):
        gates = [gates]
    rejection_reasons = (lane.get("shadow_perf") or {}).get("rejection_reasons") or {}
    ranked_rejections = sorted(
        rejection_reasons,
        key=lambda reason: (-int(rejection_reasons.get(reason) or 0), str(reason)),
    )
    return _unique([*gates, *ranked_rejections])


def _lane_projection(lane: dict[str, Any]) -> dict[str, Any]:
    lifecycle = lane.get("lifecycle") or {}
    ops_state = str(lane.get("health") or "unknown")
    setup_state = str(lifecycle.get("state") or "watching")
    ops_reasons = _unique(
        [*(lane.get("health_reasons") or []), lane.get("health_reason")]
    )
    setup_reasons = _unique(
        [
            lane.get("current_waiting_reason"),
            lane.get("last_reject_reason"),
            lane.get("why_no_fire"),
            *_failed_gates(lane),
        ]
    )
    return {
        "lane_id": lane.get("lane_id"),
        "strategy_id": lane.get("strategy_id"),
        "exchange": lane.get("exchange"),
        "symbol": lane.get("symbol"),
        "timeframe": lane.get("timeframe"),
        "ops": {
            "state": ops_state,
            "reasons": ops_reasons,
            "details": lane.get("health_details") or {},
            "candle_status": lane.get("candle_status"),
            "candle_age_ms": lane.get("candle_age_ms"),
        },
        "setup": {
            "state": setup_state,
            "armed_current": bool(lifecycle.get("armed_current")),
            "reasons": setup_reasons,
            "failed_gates": _failed_gates(lane),
            "session_state": lifecycle.get("session_state"),
            "htf_context_age_seconds": lifecycle.get("htf_context_age_seconds"),
        },
        "funnel": {
            "armed": int(lifecycle.get("armed_entries") or 0),
            "candidates": int(lifecycle.get("candidates") or 0),
            "accepted": int(lifecycle.get("accepted") or 0),
            "rejected": int(lifecycle.get("rejected") or 0),
            "cost_rejected": int(lifecycle.get("cost_rejected") or 0),
            "sizing_rejected": int(lifecycle.get("sizing_rejected") or 0),
            "risk_rejected": int(lifecycle.get("risk_rejected") or 0),
            "portfolio_rejected": int(lifecycle.get("portfolio_rejected") or 0),
            "prerequisite_rejected": int(lifecycle.get("prerequisite_rejected") or 0),
            "resolved": int(lifecycle.get("resolved") or 0),
            "pending": int(lifecycle.get("pending") or 0),
        },
        "latency": {
            "close_to_arm_ms": lane.get("close_to_arm_ms"),
            "bar_close_receipt_ms": lane.get("bar_close_receipt_ms"),
            "canonical_wait_ms": lane.get("canonical_wait_ms"),
            "decision_lag_ms": lane.get("decision_lag_ms"),
            "quote_ingest_ms": lane.get("quote_ingest_ms"),
            "acceptance_hold_ms": lane.get("acceptance_hold_ms"),
            "quote_age_at_accept_ms": lane.get("quote_age_at_accept_ms"),
            "kernel_submit_ms": lane.get("kernel_submit_ms"),
            "adapter_ack_ms": lane.get("adapter_ack_ms"),
        },
        "quotes": {
            "source": (lane.get("shadow_perf") or {}).get("quote_source"),
            "seen": int((lane.get("shadow_perf") or {}).get("quotes_seen") or 0),
            "distinct": int((lane.get("shadow_perf") or {}).get("quotes_distinct") or 0),
            "duplicates": int(
                (lane.get("shadow_perf") or {}).get("quote_contract_rejects") or 0
            ),
            "overflow_drops": int(
                (lane.get("shadow_perf") or {}).get("quote_overflow_drops") or 0
            ),
            "rearms": int((lane.get("shadow_perf") or {}).get("quote_rearms") or 0),
        },
        "runtime_contract": lane.get("runtime_contract"),
        "net": {
            "value": lifecycle.get("net_value"),
            "unit": lifecycle.get("net_unit"),
            "basis": lifecycle.get("net_basis"),
        },
    }


def _evidence_projection(
    strategy_ids: Iterable[str], catalog: dict[str, Any]
) -> dict[str, Any]:
    by_id = {
        str(row.get("strategy_id")): row
        for row in catalog.get("scanners", [])
        if row.get("strategy_id")
    }
    rows: list[dict[str, Any]] = []
    for strategy_id in strategy_ids:
        row = by_id.get(strategy_id)
        if row is None:
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "state": "untested",
                    "judgments": 0,
                    "preregistrations": [],
                    "burned_windows": [],
                    "catalogued": False,
                }
            )
            continue
        rows.append(
            {
                "strategy_id": strategy_id,
                "state": row.get("evidence") or "untested",
                "judgments": int(row.get("judgments") or 0),
                "preregistrations": row.get("preregistrations") or [],
                "burned_windows": row.get("burned_windows") or [],
                "catalogued": True,
            }
        )
    states = _unique(row["state"] for row in rows)
    if not states:
        state = "untested"
    elif len(states) == 1:
        state = states[0]
    else:
        state = "mixed"
    strongest = max(
        rows,
        key=lambda row: EVIDENCE_AUTHORITY.get(str(row["state"]), 0),
        default=None,
    )
    return {
        "state": state,
        "strongest_state": strongest["state"] if strongest else "untested",
        "exact_ids": rows,
        "has_preregistration": any(row["preregistrations"] for row in rows),
        "judgments": sum(int(row["judgments"]) for row in rows),
    }


def build_pattern_atlas_payload(
    lanes_payload: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    """Join pattern anatomy, exact runtime lanes, and evidence states."""
    all_lanes = list(lanes_payload.get("lanes") or [])
    patterns: list[dict[str, Any]] = []
    for definition in PATTERN_DEFINITIONS:
        matched = [
            _lane_projection(lane)
            for lane in all_lanes
            if lane.get("strategy_id") in definition.strategy_ids
        ]
        ops_state = max(
            (str(lane["ops"]["state"]) for lane in matched),
            key=lambda state: _OPS_RANK.get(state, 0),
            default="not_rostered",
        )
        setup_state = max(
            (str(lane["setup"]["state"]) for lane in matched),
            key=lambda state: _SETUP_RANK.get(state, 0),
            default="not_rostered",
        )
        funnel_keys = (
            "armed",
            "candidates",
            "accepted",
            "rejected",
            "cost_rejected",
            "sizing_rejected",
            "risk_rejected",
            "portfolio_rejected",
            "prerequisite_rejected",
            "resolved",
            "pending",
        )
        funnel = {
            key: sum(int(lane["funnel"].get(key) or 0) for lane in matched)
            for key in funnel_keys
        }
        net_usd = sum(
            float(lane["net"]["value"] or 0)
            for lane in matched
            if lane["net"]["unit"] == "USD"
        )
        ops_blockers = _unique(
            reason
            for lane in matched
            if lane["ops"]["state"] != "ok"
            for reason in lane["ops"]["reasons"]
        )
        setup_blockers = _unique(
            reason
            for lane in matched
            if lane["setup"]["state"] not in {"accepted", "holding", "armed"}
            for reason in lane["setup"]["reasons"]
        )
        evidence = _evidence_projection(definition.strategy_ids, catalog)
        evidence_blockers = []
        if not evidence["has_preregistration"]:
            evidence_blockers.append("no_pre_registration_linked")
        if evidence["state"] in {"untested", "mixed"}:
            evidence_blockers.append(f"evidence_{evidence['state']}")
        patterns.append(
            {
                **asdict(definition),
                "runtime": {
                    "ops_state": ops_state,
                    "setup_state": setup_state,
                    "lanes": matched,
                    "lane_count": len(matched),
                    "funnel": funnel,
                    "net_usd": net_usd,
                    "blockers": {
                        "ops": ops_blockers,
                        "setup": setup_blockers,
                        "evidence": evidence_blockers,
                    },
                },
                "evidence": evidence,
            }
        )

    return {
        "schema": "vnedge.pattern_atlas.v2",
        "generated_at": lanes_payload.get("generated_at"),
        "source_snapshot_at": lanes_payload.get("source_snapshot_at"),
        "snapshot_state": lanes_payload.get("snapshot_state", "unknown"),
        "patterns": patterns,
        "summary": {
            "patterns": len(patterns),
            "runtime_lanes": sum(pattern["runtime"]["lane_count"] for pattern in patterns),
            "ops_blocked": sum(
                1 for pattern in patterns if pattern["runtime"]["ops_state"] == "blocked"
            ),
            "active_setups": sum(
                1
                for pattern in patterns
                if pattern["runtime"]["setup_state"] in {"armed", "holding", "accepted"}
            ),
            "accepted": sum(pattern["runtime"]["funnel"]["accepted"] for pattern in patterns),
        },
        "policy": {"can_trade": False, "can_promote": False, "read_only": True},
    }
