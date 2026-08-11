import io
import json
from datetime import UTC, datetime

import joblib
import pytest

from vnedge.ml.decision_engine import MockModel
from vnedge.ml.model_registry import (
    IllegalStatusTransition,
    ModelMetadata,
    ModelNotApproved,
    ModelRegistry,
)


def _meta(model_id="m1", status="research"):
    return ModelMetadata(
        model_id=model_id, family="test", created_at=datetime.now(UTC),
        trained_on_window="2023->2025", feature_set_version="fv1",
        algorithm="mock", hyperparams={}, metrics={}, status=status,
        artifact_path=f"{model_id}.joblib", config_hash="abc",
    )


def _artifact(scores=None):
    buf = io.BytesIO()
    joblib.dump(MockModel(scores or {"long": 0.7, "short": 0.2, "edge_bps": 40, "confidence": 0.9}), buf)
    return buf.getvalue()


def test_register_and_get(tmp_path):
    reg = ModelRegistry(tmp_path)
    reg.register(_meta(), _artifact())
    assert reg.get("m1").status == "research"


def test_register_must_start_research(tmp_path):
    with pytest.raises(ValueError):
        ModelRegistry(tmp_path).register(_meta(status="paper"), _artifact())


def test_artifacts_are_immutable(tmp_path):
    reg = ModelRegistry(tmp_path)
    reg.register(_meta(), _artifact())
    with pytest.raises(FileExistsError):
        reg.register(_meta(), _artifact())          # same model_id, write-once


def test_load_artifact_status_gated(tmp_path):
    reg = ModelRegistry(tmp_path)
    reg.register(_meta(), _artifact())
    with pytest.raises(ModelNotApproved):
        reg.load_artifact("m1")                      # research → blocked
    reg.update_status("m1", "candidate", "n")
    with pytest.raises(ModelNotApproved):
        reg.load_artifact("m1")                      # candidate → blocked
    reg.update_status("m1", "paper", "n")
    model = reg.load_artifact("m1")                  # paper → allowed
    assert model.score({})["long"] == 0.7


def test_legal_transition_ladder(tmp_path):
    reg = ModelRegistry(tmp_path)
    reg.register(_meta(), _artifact())
    for s in ("candidate", "paper", "promoted"):
        reg.update_status("m1", s, "n")
    assert reg.get("m1").status == "promoted"
    assert reg.get("m1").promoted_at is not None


@pytest.mark.parametrize("bad", ["paper", "promoted", "retired"])
def test_illegal_transitions_raise(tmp_path, bad):
    reg = ModelRegistry(tmp_path)
    reg.register(_meta(), _artifact())
    with pytest.raises(IllegalStatusTransition):
        reg.update_status("m1", bad, "skip")         # research → * (only candidate/rejected legal)


def test_audit_is_appended_and_hash_chained(tmp_path):
    reg = ModelRegistry(tmp_path)
    reg.register(_meta(), _artifact())
    reg.update_status("m1", "candidate", "looks good")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2                            # register + candidate
    first, last = json.loads(lines[0]), json.loads(lines[1])
    assert last["to"] == "candidate" and last["note"] == "looks good"
    assert last["prev_hash"] == first["hash"]         # tamper-evident chain


def test_list_filters_by_status(tmp_path):
    reg = ModelRegistry(tmp_path)
    reg.register(_meta("a"), _artifact())
    reg.register(_meta("b"), _artifact())
    reg.update_status("a", "candidate", "n")
    assert {m.model_id for m in reg.list(status="candidate")} == {"a"}
    assert {m.model_id for m in reg.list(status="research")} == {"b"}
