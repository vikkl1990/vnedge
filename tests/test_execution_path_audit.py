from __future__ import annotations

from datetime import UTC, datetime

from vnedge.execution.evidence import DecisionEnvelope, ExecutionEvidence
from vnedge.runtime.canonical_parity import atomic_write_json
from vnedge.runtime.execution_contract import KERNEL_PATH_ID
from vnedge.runtime.execution_path_audit import (
    assert_execution_path_artifact,
    build_execution_path_audit,
)
from vnedge.strategy.arm_evidence import freeze_permission_from_row


def _evidence() -> ExecutionEvidence:
    snapshot = freeze_permission_from_row(
        {
            "timestamp": datetime(2026, 9, 5, 12, tzinfo=UTC),
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 10,
            "quote_volume": 1005,
            "trade_count": 2,
            "is_closed": True,
            "data_quality": "ok",
            "candle_source": "canonical_tick_lake",
        },
        decision_timeframe="15m",
        context_timeframes=(),
        allow_long=True,
        allow_short=False,
        reason="test",
    )
    decision = DecisionEnvelope.create(
        strategy_id="test_v1",
        symbol="BTC/USD:USD",
        timeframe="15m",
        side="long",
        permission_snapshot=snapshot,
        entry_clock="next_15m_open",
    )
    return ExecutionEvidence.from_decision(decision)


def test_kernel_envelope_is_the_only_execution_ready_entry() -> None:
    evidence = _evidence()
    result = build_execution_path_audit(
        [
            {
                "kind": "order_submitted",
                "payload": {
                    "path_id": KERNEL_PATH_ID,
                    "decision_id": evidence.decision_id,
                    "intent": {"reduce_only": False},
                    "execution_evidence": evidence.as_dict(),
                },
            },
            {
                "kind": "shadow_outcome",
                "payload": {"performance_eligible": False},
            },
        ],
        chain_ok=True,
    )

    assert result["execution_ready"] is True
    assert result["counts"]["kernel_submitted"] == 1


def test_observe_row_cannot_claim_kernel_authority() -> None:
    result = build_execution_path_audit(
        [{"kind": "shadow_intent", "payload": {"path_id": KERNEL_PATH_ID}}],
        chain_ok=True,
    )

    assert result["execution_ready"] is False
    assert "operational_path_leak" in result["blockers"]


def test_ready_execution_artifact_must_be_fresh_and_have_kernel_sample(tmp_path) -> None:
    evidence = _evidence()
    result = build_execution_path_audit(
        [
            {
                "kind": "order_submitted",
                "payload": {
                    "path_id": KERNEL_PATH_ID,
                    "decision_id": evidence.decision_id,
                    "intent": {"reduce_only": False},
                    "execution_evidence": evidence.as_dict(),
                },
            }
        ],
        chain_ok=True,
    )
    path = tmp_path / "audit.json"
    atomic_write_json(path, result)

    assert assert_execution_path_artifact(path)["execution_ready"] is True
