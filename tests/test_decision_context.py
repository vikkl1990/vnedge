from copy import deepcopy

import pytest

from vnedge.strategy.decision_context import explain_evaluation
from vnedge.strategy.scanner_observability import enrich_evaluation


def evaluation():
    return {"strategy_id": "htf_regime_continuation_15m_v2__BTCUSD",
            "mreg_ready": True, "structure_ready": False, "fired": False,
            "primary_failed_gate": "regime_flat",
            "all_failed_gates": ["regime_flat", "structure_not_ready"],
            "features": {"regime_state": "mean_revert", "regime_reason": "weekly_range_macd_off",
                         "permission_long": False, "permission_short": False}}


def test_context_explains_family_mismatch_without_rewriting_gates():
    record = evaluation()
    before = deepcopy(record)
    result = enrich_evaluation(record)
    assert record == before
    assert result["all_failed_gates"] == before["all_failed_gates"]
    context = result["decision_context"]
    assert context == explain_evaluation(record).to_dict()
    assert context["family_compatible"] is False
    assert "Mean-reversion" in context["explanation"]
    assert context["read_only"] is True


@pytest.mark.parametrize("value", [None, "false", "true", float("nan"), 3])
def test_unknown_regime_not_treated_as_healthy(value):
    record = {**evaluation(), "mreg_ready": value}
    context = explain_evaluation(record)
    assert context.regime_ready is None
    assert context.family_compatible is None
    assert "unproven" in context.explanation


@pytest.mark.parametrize("record,stage", [
    ({"fired": True}, "signal_detected"),
    ({"eligible": True}, "setup_detected"),
    ({"features": {"hold_confirmed": True}}, "setup_detected"),
    ({"features": {"break_long": "false", "arm_ready": float("nan")}}, "watching"),
])
def test_evaluation_cannot_claim_order_arm_or_cost_approval(record, stage):
    result = enrich_evaluation(record)
    assert result["setup_lifecycle"] == stage
    assert "decision_id" not in result


def test_empty_context_stays_unknown():
    result = explain_evaluation({})
    assert result.regime_ready is result.structure_ready is result.allow_long is None
    assert result.explanation == "Awaiting a recorded evaluation."
