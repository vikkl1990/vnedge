import io
from datetime import UTC, datetime

import joblib
import pytest

from vnedge.ml.decision_engine import MockModel
from vnedge.ml.model_registry import ModelMetadata, ModelRegistry
from vnedge.ml.promotion import PromotionError, PromotionService


def _reg_model(reg, mid="m"):
    meta = ModelMetadata(
        model_id=mid, family="test", created_at=datetime.now(UTC),
        trained_on_window="w", feature_set_version="fv", algorithm="mock",
        hyperparams={}, metrics={}, status="research",
        artifact_path=f"{mid}.joblib", config_hash="h",
    )
    buf = io.BytesIO()
    joblib.dump(MockModel({"long": 0.7}), buf)
    reg.register(meta, buf.getvalue())


def test_full_promotion_path(tmp_path):
    reg = ModelRegistry(tmp_path)
    _reg_model(reg)
    svc = PromotionService(reg)
    svc.submit_for_sealed_tail("m", {"min_sharpe": 0.5})
    svc.record_sealed_tail_result("m", {"sharpe": 0.9}, passed=True)
    assert reg.get("m").status == "candidate"
    svc.approve_for_paper("m", "alice", "looks good")
    assert reg.get("m").status == "paper"
    checklist = tmp_path / "checklist.md"
    checklist.write_text("all green")
    svc.approve_for_live("m", "bob", checklist)
    assert reg.get("m").status == "promoted"


def test_failed_sealed_tail_rejects(tmp_path):
    reg = ModelRegistry(tmp_path)
    _reg_model(reg)
    PromotionService(reg).record_sealed_tail_result("m", {"sharpe": 0.0}, passed=False)
    assert reg.get("m").status == "rejected"


def test_cannot_skip_sealed_tail_to_paper(tmp_path):
    reg = ModelRegistry(tmp_path)
    _reg_model(reg)
    with pytest.raises(PromotionError):
        PromotionService(reg).approve_for_paper("m", "alice", "n")   # still research


def test_live_requires_existing_checklist(tmp_path):
    reg = ModelRegistry(tmp_path)
    _reg_model(reg)
    svc = PromotionService(reg)
    svc.record_sealed_tail_result("m", {}, passed=True)
    svc.approve_for_paper("m", "alice", "n")
    with pytest.raises(PromotionError):
        svc.approve_for_live("m", "bob", tmp_path / "nope.md")


def test_sealed_tail_criteria_only_for_research(tmp_path):
    reg = ModelRegistry(tmp_path)
    _reg_model(reg)
    svc = PromotionService(reg)
    svc.record_sealed_tail_result("m", {}, passed=True)   # → candidate
    with pytest.raises(PromotionError):
        svc.submit_for_sealed_tail("m", {"x": 1})         # no longer research
