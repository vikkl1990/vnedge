"""One read-only explanation of an evaluation, never a permission source.

Derive from the exact recorded features, not a second regime calculation.
Unknown values remain unknown. Original gates and their order are preserved.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


def _flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value not in ("", "nan", "None") else None


@dataclass(frozen=True, slots=True)
class DecisionContext:
    strategy_id: str | None
    decision_at: str | None
    decision_bar_hash: str | None
    candle_source: str | None
    regime_ready: bool | None
    regime_state: str | None
    regime_reason: str | None
    regime_asof_close: str | None
    required_family: str | None
    family_compatible: bool | None
    structure_ready: bool | None
    allow_long: bool | None
    allow_short: bool | None
    primary_gate: str | None
    failed_gates: tuple[str, ...]
    explanation: str
    schema_version: int = 1
    read_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["failed_gates"] = list(self.failed_gates)
        return result


def explain_evaluation(record: Mapping[str, Any]) -> DecisionContext:
    features = record.get("features")
    f = features if isinstance(features, Mapping) else {}
    source = record.get("data_source")
    source = source if isinstance(source, Mapping) else {}
    gates = record.get("all_failed_gates")
    gates = tuple(str(g) for g in gates) if isinstance(gates, (tuple, list)) else ()
    primary = _text(record.get("primary_failed_gate")) or _text(record.get("skip_reason"))
    strategy_id = _text(record.get("strategy_id"))
    # Only this known family has this requirement. Never guess other contracts.
    required = "continuation" if (strategy_id or "").startswith("htf_regime_continuation_15m") else None
    ready = _flag(record.get("mreg_ready"))
    state = _text(f.get("regime_state"))
    compatible = state == required if ready is True and state and required else None
    if record.get("fired") is True:
        explanation = "Signal detected; ARM, cost approval and an order require separate evidence."
    elif primary == "regime_flat":
        if ready is not True or not state:
            explanation = "Regime context is unproven; no setup permission inferred."
        elif state == "mean_revert" and required == "continuation":
            explanation = "Mean-reversion context; this continuation scanner does not match it."
        elif state == "flat":
            explanation = "Context evaluated, but higher-timeframe conditions do not permit continuation."
        else:
            explanation = f"Regime gate rejected in state {state}; inspect the recorded conditions."
    elif primary in {"htf_context_missing", "market_regime_not_ready", "structure_parent_missing"}:
        explanation = "Required context is missing or not ready; this is not a market-direction verdict."
    elif primary == "structure_not_ready":
        explanation = "Required confirmed structure is not ready; no setup can be armed."
    elif primary:
        explanation = "Blocked: " + primary.replace("_", " ") + "."
    elif record:
        explanation = "No signal recorded; no execution approval inferred."
    else:
        explanation = "Awaiting a recorded evaluation."
    return DecisionContext(
        strategy_id=strategy_id, decision_at=_text(record.get("decision_at")),
        decision_bar_hash=_text(source.get("decision_row_sha256")),
        candle_source=_text(source.get("candle_source")), regime_ready=ready,
        regime_state=state, regime_reason=_text(f.get("regime_reason")),
        regime_asof_close=_text(f.get("regime_asof_close_time")),
        required_family=required, family_compatible=compatible,
        structure_ready=_flag(record.get("structure_ready")),
        allow_long=_flag(f.get("permission_long")), allow_short=_flag(f.get("permission_short")),
        primary_gate=primary, failed_gates=gates, explanation=explanation,
    )
