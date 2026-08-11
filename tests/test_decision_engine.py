import io
from datetime import UTC, datetime

import joblib
import pytest

from vnedge.ml.decision_engine import (
    ArmIntent,
    DecisionEngine,
    DecisionEngineConfig,
    MockModel,
    arm_to_signal_intent,
)
from vnedge.ml.model_registry import ModelMetadata, ModelNotApproved, ModelRegistry


def _register(reg, mid, scores, *, promote_to="paper"):
    meta = ModelMetadata(
        model_id=mid, family="test", created_at=datetime.now(UTC),
        trained_on_window="w", feature_set_version="fv", algorithm="mock",
        hyperparams={}, metrics={}, status="research",
        artifact_path=f"{mid}.joblib", config_hash="h",
    )
    buf = io.BytesIO()
    joblib.dump(MockModel(scores), buf)
    reg.register(meta, buf.getvalue())
    ladder = {"candidate": ["candidate"], "paper": ["candidate", "paper"]}
    for s in ladder.get(promote_to, []):
        reg.update_status(mid, s, "n")


def test_engine_refuses_unapproved_model(tmp_path):
    reg = ModelRegistry(tmp_path)
    _register(reg, "m", {"long": 0.7, "short": 0.2, "edge_bps": 40}, promote_to="research")
    with pytest.raises(ModelNotApproved):
        DecisionEngine(reg, ["m"])                   # research model can't be loaded


def test_engine_emits_arm_when_thresholds_pass(tmp_path):
    reg = ModelRegistry(tmp_path)
    _register(reg, "m", {"long": 0.7, "short": 0.2, "edge_bps": 40, "confidence": 0.9})
    arm = DecisionEngine(reg, ["m"]).decide({"f": 1.0})
    assert isinstance(arm, ArmIntent)
    assert arm.side == "long" and arm.probability == 0.7 and arm.expected_edge_bps == 40


def test_engine_none_below_probability(tmp_path):
    reg = ModelRegistry(tmp_path)
    _register(reg, "m", {"long": 0.55, "short": 0.40, "edge_bps": 40, "confidence": 0.9})
    assert DecisionEngine(reg, ["m"]).decide({"f": 1.0}) is None   # 0.55 < 0.60


def test_engine_none_when_cost_not_cleared(tmp_path):
    reg = ModelRegistry(tmp_path)
    _register(reg, "m", {"long": 0.7, "short": 0.2, "confidence": 0.9})   # no edge_bps
    assert DecisionEngine(reg, ["m"]).decide({"f": 1.0}) is None


def test_engine_none_below_edge(tmp_path):
    reg = ModelRegistry(tmp_path)
    _register(reg, "m", {"long": 0.7, "short": 0.2, "edge_bps": 5, "confidence": 0.9})
    cfg = DecisionEngineConfig(min_expected_edge_bps=20.0)
    assert DecisionEngine(reg, ["m"], cfg).decide({"f": 1.0}) is None


def test_arm_to_signal_intent_attaches_stop_and_target():
    arm = ArmIntent(side="long", strength=0.7, probability=0.7, expected_edge_bps=40,
                    invalidation_price=None, model_id="m", reason="r")
    si = arm_to_signal_intent(arm, close=100.0, atr=2.0)
    assert si.side == "long"
    assert si.stop_price < 100.0 < si.take_profit_price
