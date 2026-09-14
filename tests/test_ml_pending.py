"""Pending ML plumbing: synthetic validation only, never evidence of edge."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from vnedge.ml.lab_validation import development_validation, event_purged_splits, family_statistics
from vnedge.research.funding_evidence import archive_history, collect_once


def test_funding_archive_is_immutable_and_not_settlement_proof(tmp_path):
    raw = json.dumps(
        {
            "success": True,
            "result": [{"time": 3600, "open": 0.01, "close": 0.02, "high": 0.03, "low": -0.01}],
        }
    ).encode()
    first = archive_history(tmp_path, "BTCUSD", 1, 7200, raw)
    assert first == archive_history(tmp_path, "BTCUSD", 1, 7200, raw)
    assert len(list((tmp_path / "archives").glob("*.json"))) == 1
    assert first["settlement_verified"] is False
    changed = archive_history(tmp_path, "BTCUSD", 1, 7200, raw.replace(b"0.02", b"0.025"))
    assert changed["archive_id"] != first["archive_id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "result": []},
        {"success": True, "result": [{"time": 99999}]},
        {"success": True, "result": [{"time": 1, "open": "NaN"}]},
    ],
)
def test_funding_invalid_history_never_archived(tmp_path, payload):
    with pytest.raises((ValueError, KeyError)):
        archive_history(tmp_path, "BTCUSD", 1, 7200, json.dumps(payload).encode())
    assert not list(tmp_path.rglob("*.json"))


def test_funding_network_failure_stays_visible(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("network unavailable")

    monkeypatch.setattr("vnedge.research.funding_evidence.fetch_history", fail)
    status = collect_once(tmp_path)
    assert len(status["markets"]) == 2
    assert all(
        not m["settlement_verified"] and not m["history_archived"] for m in status["markets"]
    )
    assert json.loads((tmp_path / "status.json").read_text())["can_trade"] is False


def rows(n=900):
    start = datetime(2026, 9, 14, tzinfo=UTC)
    return [
        {
            "decision_id": str(i),
            "decision_at": (start + timedelta(hours=i)).isoformat(),
            "available_at": (start + timedelta(hours=i, minutes=10)).isoformat(),
            "features": {"ret_1": float(i % 2)},
            "target": i % 2,
            "net_bps": 10 if i % 2 else -12,
        }
        for i in range(n)
    ]


def test_cpcv_purges_actual_long_availability_and_embargo():
    data = rows(60)
    data[0]["available_at"] = data[35]["available_at"]
    for train, test in event_purged_splits(data, 3600):
        assert not set(train) & set(test)
        for i in train:
            for j in test:
                a, b = (
                    datetime.fromisoformat(data[i]["decision_at"]),
                    datetime.fromisoformat(data[i]["available_at"]),
                )
                c, d = (
                    datetime.fromisoformat(data[j]["decision_at"]),
                    datetime.fromisoformat(data[j]["available_at"]),
                )
                assert a > d + timedelta(hours=1) or b + timedelta(hours=1) < c


def test_development_cpcv_never_reads_final_holdout_and_counts_unique(monkeypatch):
    class Model:
        def predict_proba_up(self, frame):
            assert np.isfinite(frame.values).all()
            return np.where(frame["ret_1"] > 0.5, 0.7, 0.3)

    def train(frame, target, **kw):
        assert len(frame) >= 200
        assert np.isfinite(frame.values).all()
        return Model()

    monkeypatch.setattr("vnedge.ml.trainer.train_classifier", train)
    data = rows()
    boundary = datetime.fromisoformat(data[-1]["available_at"]) + timedelta(hours=1)
    plan = SimpleNamespace(
        test_start=boundary,
        embargo_seconds=0,
        min_train=200,
        min_calibration=50,
        features=("ret_1",),
        probability_threshold=0.6,
    )
    poisoned = rows(901)[-1] | {"features": {"ret_1": float("nan")}}
    result = development_validation(data + [poisoned], plan)
    assert len(result["folds"]) == 15
    assert result["unique_decisions"] == 900


def test_family_ties_cannot_create_a_winner():
    values = np.random.default_rng(9).normal(size=64)
    report = family_statistics(np.column_stack([values, values]))
    assert report["pbo"] == 0.5
    assert report["raw_trials"] == 2


@pytest.mark.parametrize("matrix", [np.ones((64, 2)), np.zeros((2, 2)), np.full((64, 2), np.nan)])
def test_family_missing_information_refused(matrix):
    with pytest.raises(ValueError):
        family_statistics(matrix)


def test_family_registration_must_precede_every_attempt(tmp_path):
    from vnedge.ml.lab_pipeline import COHORT_FIELDS, LabPlan, register_plan
    from vnedge.ml.lab_validation import register_family, validate_family

    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    cohort = dict.fromkeys(COHORT_FIELDS, "fixture") | {
        "mode": "paper",
        "label_contract": "reconciled_paper_net_positive_v1",
    }
    body = {
        "name": "fixture",
        "cohort": cohort,
        "features": ("ret_1",),
        "train_start": now,
        "calibration_start": now + timedelta(days=1),
        "test_start": now + timedelta(days=2),
        "test_end": now + timedelta(days=40),
    }
    first = register_plan(tmp_path, LabPlan.model_validate(body))
    second = register_plan(
        tmp_path, LabPlan.model_validate(body | {"name": "second", "probability_threshold": 0.7})
    )
    family = register_family(tmp_path, [first, second])
    with pytest.raises(OSError):  # no result must never be silently dropped
        validate_family(tmp_path, family)
    (tmp_path / "runs" / first).mkdir(parents=True)
    with pytest.raises(ValueError, match="precede"):
        register_family(tmp_path, [first, second])
