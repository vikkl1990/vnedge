"""Read-only scanner lifecycle, near-miss, routing, and conflict evidence.

This module is deliberately downstream of a strategy evaluation.  It cannot
mint an order, alter a strategy threshold, or grant capital permission.  Its
job is to make every scanner evaluation explainable and comparable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any


class SetupLifecycle(str, Enum):
    WATCHING = "watching"
    ARMED = "armed"
    BREAK_DETECTED = "break_detected"
    ACCEPTED = "accepted"
    COST_APPROVED = "cost_approved"
    SHADOW_INTENT = "shadow_intent"
    OUTCOME = "outcome"


_BREAK_TOKENS = ("break", "sweep", "reclaim", "cross")
_ACCEPT_TOKENS = ("accept", "hold", "confirmation")
_COST_TOKENS = ("cost", "projected_net", "edge")


def _flags(features: Mapping[str, Any], tokens: tuple[str, ...]) -> bool:
    return any(bool(value) and any(token in str(name).lower() for token in tokens)
               for name, value in features.items())


def classify_lifecycle(
    *,
    fired: bool,
    eligible: bool,
    failed_gates: Sequence[str],
    features: Mapping[str, Any],
    signal_reason: str | None = None,
) -> SetupLifecycle:
    """Infer the furthest completed setup stage from explicit evidence."""
    if fired:
        return SetupLifecycle.SHADOW_INTENT
    if eligible:
        return SetupLifecycle.COST_APPROVED
    if _flags(features, _ACCEPT_TOKENS):
        return SetupLifecycle.ACCEPTED
    if _flags(features, _BREAK_TOKENS) or signal_reason:
        return SetupLifecycle.BREAK_DETECTED
    # An arm-ready/compressed/setup flag means the market has completed the
    # scanner's setup, even when the later trigger is absent.
    if _flags(features, ("arm_ready", "compressed", "setup_ready", "trend_ok")):
        return SetupLifecycle.ARMED
    return SetupLifecycle.WATCHING


def build_near_miss(
    failed_gates: Sequence[str], distances: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Return a deterministic closest-gate explanation; never changes a gate."""
    finite: list[tuple[str, float]] = []
    for name, raw in distances.items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0 and value < float("inf"):
            finite.append((str(name), value))
    finite.sort(key=lambda item: (item[1], item[0]))
    if not failed_gates and not finite:
        return None
    closest = finite[0] if finite else None
    return {
        "failed_gate_count": len(failed_gates),
        "closest_metric": closest[0] if closest else None,
        "closest_distance": closest[1] if closest else None,
        "counterfactual_only": True,
        "would_pass_if": (
            f"{closest[0]} reached zero" if closest else f"{failed_gates[0]} passed"
        ),
    }


@dataclass(frozen=True, slots=True)
class ScannerCandidate:
    strategy_id: str
    symbol: str
    side: str
    score: float
    lifecycle: SetupLifecycle


def arbitrate_conflicts(candidates: Sequence[ScannerCandidate]) -> dict[str, Any]:
    """Resolve same-symbol observations for display only.

    Opposing directions produce a conflict and no selected candidate.  Same
    direction candidates are ranked by lifecycle then declared score.
    """
    if not candidates:
        return {"state": "empty", "selected": None, "candidates": []}
    sides = {candidate.side for candidate in candidates}
    rows = [
        {
            "strategy_id": item.strategy_id,
            "symbol": item.symbol,
            "side": item.side,
            "score": item.score,
            "lifecycle": item.lifecycle.value,
        }
        for item in candidates
    ]
    if len(sides) > 1:
        return {"state": "conflict", "selected": None, "candidates": rows}
    rank = {state: index for index, state in enumerate(SetupLifecycle)}
    selected = max(candidates, key=lambda item: (rank[item.lifecycle], item.score))
    return {
        "state": "aligned" if len(candidates) > 1 else "single",
        "selected": selected.strategy_id,
        "side": selected.side,
        "candidates": rows,
        "read_only": True,
    }


def enrich_evaluation(record: Mapping[str, Any]) -> dict[str, Any]:
    """Attach stable observability fields to a lane-evaluation record."""
    enriched = dict(record)
    features = record.get("features")
    features = features if isinstance(features, Mapping) else {}
    failed = record.get("all_failed_gates")
    failed = failed if isinstance(failed, (list, tuple)) else []
    distances = record.get("distance_to_threshold")
    distances = distances if isinstance(distances, Mapping) else {}
    lifecycle = classify_lifecycle(
        fired=bool(record.get("fired")),
        eligible=bool(record.get("eligible")),
        failed_gates=[str(item) for item in failed],
        features=features,
        signal_reason=(str(record["signal_reason"]) if record.get("signal_reason") else None),
    )
    enriched["setup_lifecycle"] = lifecycle.value
    enriched["near_miss"] = build_near_miss([str(item) for item in failed], distances)
    enriched["observability_version"] = 1
    return enriched


__all__ = [
    "ScannerCandidate", "SetupLifecycle", "arbitrate_conflicts",
    "build_near_miss", "classify_lifecycle", "enrich_evaluation",
]
