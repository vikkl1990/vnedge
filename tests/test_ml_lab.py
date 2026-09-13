import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.ml_lab import ml_lab_payload
from vnedge.ml.lab_audit import audit_records, build_ml_lab_audit
from vnedge.ml.feature_matrix import FEATURE_COLUMNS
from vnedge.research.ml_pipeline_status import build_ml_pipeline_status
from tests.test_signal_queue import arm, rec


def feature(**overrides):
    proof = arm()
    return {"v": 2, "fingerprint": "abc123", "ts": "2026-09-05T12:15:01+00:00",
            "bar_ts": "2026-09-05T12:00:00+00:00", "strategy_id": proof["strategy_id"],
            "symbol": proof["symbol"], "timeframe": "15m", "exchange": "delta_india",
            "lane": "lane", "side": "long", "decision": "fired", "backfill": False,
            "decision_id": proof["decision_id"], "decision_bar_hash": proof["decision_bar_content_hash"],
            "features": {c: 0.0 for c in FEATURE_COLUMNS}, **overrides}


def test_exact_join_is_not_a_live_prediction_or_a_label():
    report = audit_records({"lane": [rec(proof=True)]}, {"lane": [feature()]})
    assert report["counts"]["exact_feature_matches"] == 1
    assert report["exclusions"]["post_decision_feature_not_live_prediction"] == 1
    assert report["operational_labels"] == 0
    assert report["training"]["trainable"] is False
    assert report["validation"]["auc"] is None


def test_duplicates_conflicts_and_cross_lane_do_not_create_samples():
    f = feature()
    report = audit_records({"lane": [rec(proof=True), rec(proof=True)]}, {"lane": [f, f]})
    assert report["counts"]["exact_feature_matches"] == 1
    assert report["counts"]["duplicate_feature_records"] == 1
    assert report["counts"]["duplicate_journal_records"] == 1
    report = audit_records({"lane": [rec(proof=True)]}, {"lane": [f, feature(fingerprint="other")]})
    assert report["counts"]["exact_feature_matches"] == 0
    assert report["exclusions"]["conflicting_feature_rows"] == 1
    report = audit_records({"lane": [rec(proof=True)]}, {"other": [f]})
    assert report["counts"]["exact_feature_matches"] == 0


def test_wrong_bar_backfill_and_missing_features_visible():
    report = audit_records({"lane": [rec(proof=True)]}, {"lane": [feature(decision_bar_hash="bad", features={})]})
    assert report["exclusions"]["feature_identity_mismatch"] == 1
    assert all(x["missing_rows"] == 1 for x in report["feature_missingness"])
    report = audit_records({}, {"lane": [feature(backfill=True)]})
    assert report["exclusions"]["feature_backfill_or_unknown"] == 1
    broken = rec(proof=True)
    broken["payload"]["execution_evidence"]["arm_envelope"]["side"] = "short"
    report = audit_records({"lane": [rec(proof=True), broken, {"kind": {}}]}, {"lane": [feature()]})
    assert report["counts"]["bound_decisions"] == 0
    assert report["counts"]["exact_feature_matches"] == 0
    assert report["exclusions"]["invalid_journal_kind"] == 1


def test_exit_intents_and_research_results_are_not_operational_labels():
    report = audit_records({"lane": [rec("shadow_outcome", virtual_net_usd=1000),
        rec("live_paper_exit", performance_eligible=True, state="filled", final=True, net_usd=100)]}, {})
    assert report["counts"]["research_outcomes"] == 1
    assert report["counts"]["exit_records"] == 1
    assert report["operational_labels"] == 0
    assert report["exclusions"]["resolved_ledger_label_not_bound"] == 1


def test_partitions_feature_contract_and_market():
    report = audit_records({}, {"lane": [feature(), feature(symbol="ETH/USD:USD"), feature(fingerprint="other")]})
    assert len(report["cohorts"]) == 3
    assert all(not c["trainable"] for c in report["cohorts"])


def test_bounded_files_bad_records_and_no_candle_fallback(tmp_path, monkeypatch):
    from vnedge.ml import lab_audit
    from vnedge.research import ml_pipeline_status
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy candle builder must not be called")
    monkeypatch.setattr(ml_pipeline_status, "_load_candles", forbidden)
    monkeypatch.setattr(ml_pipeline_status, "build_meta_label_dataset", forbidden)
    (tmp_path / "lane.features.jsonl").write_text(json.dumps(feature()) + "\n{bad}\n{tail")
    result = build_ml_pipeline_status(lane_dir=tmp_path, data_root=tmp_path)
    assert result["audit"]["counts"]["feature_rows"] == 1
    assert result["audit"]["sources"][0]["invalid"] == 1
    assert result["audit"]["sources"][0]["incomplete_tail"] is True
    assert result["validation"] is None and result["dataset"]["win_rate_pct"] is None
    monkeypatch.setattr(lab_audit, "READ_BYTES", 100)
    report = build_ml_lab_audit(tmp_path)
    assert report["sources"][0]["truncated"] is True
    assert report["history_complete"] is False


def test_artifact_freshness_legacy_and_invalid_states(tmp_path):
    path = tmp_path / "status.json"
    now = datetime.now(UTC)
    assert ml_lab_payload(path)["artifact_state"] == "MISSING"
    path.write_text(json.dumps({"dataset": {"samples": 1000}, "validation": {"passed": True}}))
    assert ml_lab_payload(path)["artifact_state"] == "LEGACY_UNVERIFIED"
    status = build_ml_pipeline_status(lane_dir=tmp_path, data_root=tmp_path)
    status["generated_at"] = (now - timedelta(hours=3)).isoformat()
    path.write_text(json.dumps(status))
    assert ml_lab_payload(path, now=now)["artifact_state"] == "STALE"
    status["generated_at"] = (now + timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(status))
    assert ml_lab_payload(path, now=now)["artifact_state"] == "INVALID"
    path.write_text("[]")
    assert ml_lab_payload(path)["artifact_state"] == "INVALID"


def test_api_read_only_and_authenticated(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PUBLIC_READ_ONLY", raising=False)
    with TestClient(create_app(SnapshotProvider(), token="test", ml_pipeline_status_path=tmp_path / "missing.json")) as client:
        assert client.get("/api/ml-lab").status_code == 401
        result = client.get("/api/ml-lab", headers={"Authorization": "Bearer test"})
        assert result.status_code == 200
        assert result.json()["can_train"] is False
        assert result.json()["audit"] is None
        assert client.post("/api/ml-lab", headers={"Authorization": "Bearer test"}).status_code == 405
