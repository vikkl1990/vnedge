"""Controlled ML Lab experiments. This module has no execution imports/actions.

Plans precede prospective windows, datasets and attempts are write-once, splits
purge by outcome *availability* (not row offsets). Calibration never sees test
labels. A measured advantage is a research result, never promotion authority.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.execution.evidence import DecisionEnvelope
from vnedge.ml.feature_log import unavailable_features
from vnedge.ml.ledger_labels import build_ledger_labels, digest, read_records, timestamp
from vnedge.ml.trainer import train_classifier

COHORT_FIELDS = (
    "strategy_id",
    "exchange",
    "symbol",
    "timeframe",
    "mode",
    "entry_clock",
    "cost_profile_id",
    "cost_config_sha256",
    "label_contract",
    "feature_fingerprint",
)


class LabPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    purpose: str = "exploratory"
    cohort: dict[str, str]
    features: tuple[str, ...]
    train_start: datetime
    calibration_start: datetime
    test_start: datetime
    test_end: datetime
    embargo_seconds: int = Field(default=3600, ge=0)
    min_train: int = Field(default=200, ge=200)
    min_calibration: int = Field(default=50, ge=50)
    min_test: int = Field(default=50, ge=50)
    probability_threshold: float = Field(default=0.60, ge=0.5, le=1)
    development_cpcv: bool = False  # frozen before fitting; never retrofitted onto a winner

    @model_validator(mode="after")
    def valid_contract(self) -> LabPlan:
        if self.purpose not in {"exploratory", "prospective"}:
            raise ValueError("judgment uses the separate human promotion process")
        if set(self.cohort) != set(COHORT_FIELDS) or not all(self.cohort.values()):
            raise ValueError("complete exact cohort required")
        if (
            self.cohort["mode"] != "paper"
            or self.cohort["label_contract"] != "reconciled_paper_net_positive_v1"
        ):
            raise ValueError("only ledger-bound paper labels supported")
        if not self.features or len(set(self.features)) != len(self.features):
            raise ValueError("unique named features required")
        from vnedge.ml.feature_matrix import FEATURE_COLUMNS

        if not set(self.features) <= set(FEATURE_COLUMNS):
            raise ValueError("unregistered feature")
        dates = [self.train_start, self.calibration_start, self.test_start, self.test_end]
        if any(t.tzinfo is None for t in dates) or not all(a < b for a, b in pairwise(dates)):
            raise ValueError("strict chronological timezone-aware windows required")
        # Historical charter judgment windows are not available to this runner.
        if self.train_start < datetime(2026, 9, 13, tzinfo=UTC):
            raise ValueError("historical protected/seen windows are outside this new pipeline")
        return self


def write_once(path: Path, value: dict) -> None:
    """Exclusive creation; a torn artifact remains an explicit failed attempt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_object(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("symlink_refused")
    with path.open("rb") as stream:
        raw = stream.read(64_000_001)
    if len(raw) > 64_000_000:
        raise ValueError("artifact_too_large")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise TypeError("object_required")
    digest(result)
    return result


def register_plan(root: Path, plan: LabPlan) -> str:
    now = datetime.now(UTC)
    if plan.purpose == "prospective" and plan.test_start <= now:
        raise ValueError("prospective_test_must_be_in_future_at_registration")
    body = plan.model_dump(mode="json")
    plan_id = digest(body)
    record = {
        "plan_id": plan_id,
        "registered_at": now.isoformat(),
        "plan": body,
        "implementation_hash": implementation_hash(),
    }
    if plan.purpose == "prospective":
        # No second reservation on an overlapping test period for the same
        # cohort, even with different thresholds or feature subsets.
        import fcntl

        root.mkdir(parents=True, exist_ok=True)
        with (root / "reservation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for path in (root / "plans").glob("*.json"):
                old = read_object(path)["plan"]
                if (
                    old["purpose"] == "prospective"
                    and old["cohort"] == body["cohort"]
                    and timestamp(old["test_start"]) < plan.test_end
                    and plan.test_start < timestamp(old["test_end"])
                ):
                    raise ValueError("prospective_window_already_reserved")
            write_once(root / "plans" / f"{plan_id}.json", record)
    else:
        write_once(root / "plans" / f"{plan_id}.json", record)
    return plan_id


def load_plan(root: Path, plan_id: str) -> tuple[LabPlan, dict]:
    validate_id(plan_id)
    record = read_object(root / "plans" / f"{plan_id}.json")
    if record["plan_id"] != plan_id or digest(record["plan"]) != plan_id:
        raise ValueError("plan_hash_mismatch")
    if record.get("implementation_hash") != implementation_hash():
        raise ValueError("implementation_changed_since_registration")
    plan = LabPlan.model_validate(record["plan"])
    if plan.purpose == "prospective" and timestamp(record["registered_at"]) >= plan.test_start:
        raise ValueError("late_preregistration")
    return plan, record


def implementation_hash() -> str:
    import sklearn

    from vnedge.ml import (
        feature_log,
        feature_matrix,
        lab_validation,
        ledger_labels,
        trainer,
        validation,
    )

    return digest(
        {
            "files": [
                hashlib_file(Path(p))
                for p in (
                    __file__,
                    trainer.__file__,
                    ledger_labels.__file__,
                    feature_log.__file__,
                    feature_matrix.__file__,
                    lab_validation.__file__,
                    validation.__file__,
                )
            ],
            "sklearn": sklearn.__version__,
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "params": trainer.DEFAULT_MODEL_PARAMS | {"early_stopping": False},
        }
    )


def validate_id(value: str) -> None:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("invalid_artifact_id")


def collect_dataset(lane_dir: Path, plan: LabPlan) -> dict:
    """Full verified histories, with one exact feature record per decision."""
    rows, sources = [], []
    rejected: Counter = Counter()
    if not lane_dir.is_dir():
        rejected["lane_directory_missing"] += 1
    for journal in sorted(lane_dir.glob("*.journal.jsonl")):
        lane = journal.name.removesuffix(".journal.jsonl")
        label_report = build_ledger_labels(journal, lane_dir / f"{lane}.fills.jsonl")
        rejected.update(label_report["rejections"])
        sources.append(
            {"lane": lane, "state": label_report["state"], "hash": label_report.get("source_hash")}
        )
        if not label_report["labels"]:
            continue
        try:
            features = read_records(lane_dir / f"{lane}.features.jsonl")
            grouped: dict[str, dict[str, dict]] = {}
            for feature in features:
                grouped.setdefault(str(feature.get("decision_id")), {})[digest(feature)] = feature
            for label in label_report["labels"]:
                candidates = grouped.get(label["decision_id"], {})
                if len(candidates) != 1:
                    rejected["missing_or_conflicting_feature_identity"] += 1
                    continue
                feature = next(iter(candidates.values()))
                try:
                    rows.append(join_label(label, feature, lane, plan))
                except (ValueError, KeyError, TypeError) as exc:
                    rejected[str(exc)] += 1
        except (OSError, ValueError, TypeError) as exc:
            rejected[str(exc)] += 1
    counts = Counter(r["decision_id"] for r in rows)
    rejected["cross_lane_duplicate_decision"] += sum(n for n in counts.values() if n > 1)
    rows = sorted(
        (r for r in rows if counts[r["decision_id"]] == 1),
        key=lambda r: (r["decision_at"], r["decision_id"]),
    )
    body = {
        "schema": "ml_frozen_dataset_v1",
        "cohort": plan.cohort,
        "features": list(plan.features),
        "rows": rows,
        "sources": sources,
        "rejections": {k: v for k, v in rejected.items() if v},
    }
    return {**body, "dataset_id": digest(body)}


def join_label(label: dict, feature: dict, lane: str, plan: LabPlan) -> dict:
    envelope = DecisionEnvelope.from_dict(label["arm_envelope"])
    cohort = {k: label.get(k) for k in COHORT_FIELDS}
    cohort["feature_fingerprint"] = feature.get("fingerprint")
    if cohort != plan.cohort:
        raise ValueError("cohort_mismatch")
    if (
        feature.get("v") != 2
        or feature.get("lane") != lane
        or feature.get("backfill") is not False
        or feature.get("decision") != "fired"
    ):
        raise ValueError("nonoperational_feature")
    expected = {
        "decision_id": envelope.decision_id,
        "decision_bar_hash": envelope.decision_bar_content_hash,
        "side": envelope.side,
        "symbol": envelope.symbol,
        "strategy_id": envelope.strategy_id,
        "timeframe": envelope.timeframe,
        "exchange": label["exchange"],
    }
    if (
        any(feature.get(k) != v for k, v in expected.items())
        or timestamp(feature["bar_ts"]) != envelope.bar_open
    ):
        raise ValueError("feature_identity_mismatch")
    close = envelope.permission_snapshot.decision_bar.close_time
    # Retrospectively recomputed vectors are not entry-time model inputs.
    if (
        not close
        <= timestamp(feature["captured_at"])
        <= timestamp(feature["ts"])
        <= timestamp(label["entry_at"])
    ):
        raise ValueError("feature_unavailable_before_entry")
    values = {name: feature["features"][name] for name in plan.features}
    if any(type(v) not in (int, float) or not np.isfinite(v) for v in values.values()):
        raise ValueError("incomplete_selected_features")
    if set(plan.features) & set(
        unavailable_features(feature["features"], feature.get("optional_inputs", []))
    ):
        raise ValueError("selected_feature_input_unavailable")
    if not plan.train_start <= close < plan.test_end:
        raise ValueError("outside_registered_window")
    return {
        "decision_id": envelope.decision_id,
        "decision_at": close.isoformat(),
        "entry_at": label["entry_at"],
        "exit_at": label["exit_at"],
        "available_at": label["available_at"],
        "label_hash": label["label_hash"],
        "feature_hash": digest(feature),
        "features": values,
        "target": label["target"],
        "net_bps": label["net_bps"],
    }


def freeze_dataset(root: Path, plan_id: str, lane_dir: Path) -> str:
    plan, _ = load_plan(root, plan_id)
    dataset = collect_dataset(lane_dir, plan)
    dataset_id = dataset["dataset_id"]
    path = root / "datasets" / f"{dataset_id}.json"
    if path.exists():
        if read_object(path) != dataset:
            raise ValueError("dataset_collision")
    else:
        write_once(path, dataset)
    return dataset_id


def temporal_split(rows: list[dict], plan: LabPlan) -> dict[str, list[dict]]:
    splits: dict[str, list[dict]] = {"train": [], "calibration": [], "test": []}
    for row in rows:
        t, end = timestamp(row["decision_at"]), timestamp(row["available_at"])
        exit_at = timestamp(row["exit_at"])
        if end < exit_at or exit_at < timestamp(row["entry_at"]) or timestamp(row["entry_at"]) < t:
            raise ValueError("invalid_label_event_clock")
        for name, start, stop in (
            ("train", plan.train_start, plan.calibration_start),
            ("calibration", plan.calibration_start, plan.test_start),
            ("test", plan.test_start, plan.test_end),
        ):
            if start <= t < stop and end + timedelta(seconds=plan.embargo_seconds) < stop:
                splits[name].append(row)
    return splits


def _metrics(rows: list[dict], probabilities: np.ndarray, threshold: float) -> dict:
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

    y = np.array([r["target"] for r in rows])
    returns = np.array([r["net_bps"] for r in rows])
    selected = probabilities >= threshold

    def stats(x: np.ndarray) -> dict:
        equity = np.r_[0.0, np.cumsum(x)]
        drawdown = np.maximum.accumulate(equity) - equity
        losses = -x[x < 0].sum()
        return {
            "n": len(x),
            "mean_net_bps": float(x.mean()) if len(x) else None,
            "sum_net_bps": float(x.sum()),
            "max_drawdown_sum_bps": float(drawdown.max()),
            "profit_factor": float(x[x > 0].sum() / losses) if losses > 0 else None,
        }

    bins = []
    for i in range(10):
        mask = (probabilities >= i / 10) & (
            probabilities < (i + 1) / 10 if i < 9 else probabilities <= 1
        )
        bins.append(
            {
                "lower": i / 10,
                "n": int(mask.sum()),
                "predicted": float(probabilities[mask].mean()) if mask.any() else None,
                "observed": float(y[mask].mean()) if mask.any() else None,
            }
        )
    return {
        "auc": float(roc_auc_score(y, probabilities)) if len(set(y)) == 2 else None,
        "brier": float(brier_score_loss(y, probabilities)),
        "log_loss": float(log_loss(y, probabilities, labels=[0, 1])),
        "baseline": stats(returns),
        "selected": stats(returns[selected]),
        "coverage": float(selected.mean()),
        "calibration_bins": bins,
        "return_basis": "unweighted_trade_net_bps_not_portfolio_equity",
    }


def run_experiment(root: Path, plan_id: str, dataset_id: str) -> dict:
    """Explicit CLI action. Never invoked by a GET, poll, or operational lane."""
    plan, registration = load_plan(root, plan_id)
    validate_id(dataset_id)
    data = read_object(root / "datasets" / f"{dataset_id}.json")
    if (
        data["dataset_id"] != dataset_id
        or digest({k: v for k, v in data.items() if k != "dataset_id"}) != dataset_id
    ):
        raise ValueError("dataset_hash_mismatch")
    if data["cohort"] != plan.cohort or data["features"] != list(plan.features):
        raise ValueError("dataset_contract_mismatch")
    if plan.test_end > datetime.now(UTC):
        raise ValueError("test_window_not_mature")
    splits = temporal_split(data["rows"], plan)
    for name, floor in (
        ("train", plan.min_train),
        ("calibration", plan.min_calibration),
        ("test", plan.min_test),
    ):
        if len(splits[name]) < floor:
            raise ValueError(f"{name}_rows_below_{floor}")
        if name != "test" and len({r["target"] for r in splits[name]}) < 2:
            raise ValueError(f"{name}_single_class")
    # Reserve BEFORE fitting. Crash/failure consumes the attempt, too.
    attempt = root / "runs" / plan_id
    attempt.mkdir(parents=True, exist_ok=False)
    _sync_directory(attempt.parent)
    write_once(
        attempt / "started.json",
        {"plan_id": plan_id, "dataset_id": dataset_id, "started_at": datetime.now(UTC).isoformat()},
    )
    try:
        import joblib
        from sklearn.linear_model import LogisticRegression

        cpcv = None
        if plan.development_cpcv:
            from vnedge.ml.lab_validation import development_validation

            cpcv = development_validation(data["rows"], plan)

        def matrix(rows: list[dict]) -> pd.DataFrame:
            return pd.DataFrame([r["features"] for r in rows], columns=plan.features)

        from threadpoolctl import threadpool_limits

        with threadpool_limits(limits=2):
            trained = train_classifier(
                matrix(splits["train"]),
                pd.Series([r["target"] for r in splits["train"]]),
                params={"early_stopping": False},
                compute_importances=False,
            )
        raw_cal = trained.predict_proba_up(matrix(splits["calibration"]))
        calibrator = LogisticRegression(random_state=7)
        calibrator.fit(_logit(raw_cal), [r["target"] for r in splits["calibration"]])
        raw_test = trained.predict_proba_up(matrix(splits["test"]))
        probabilities = calibrator.predict_proba(_logit(raw_test))[:, 1]
        bundle = {
            "model": trained,
            "calibrator": calibrator,
            "plan_id": plan_id,
            "dataset_id": dataset_id,
        }
        joblib.dump(bundle, attempt / "research_model.joblib")
        with (attempt / "research_model.joblib").open("rb") as model_stream:
            os.fsync(model_stream.fileno())
        model_hash = hashlib_file(attempt / "research_model.joblib")
        test_predictions = {
            "predictions": [
                {
                    "decision_id": r["decision_id"],
                    "probability": float(p),
                    "kind": "retrospective_test_not_forward",
                }
                for r, p in zip(splits["test"], probabilities)
            ]
        }
        result = {
            "schema": "ml_lab_run_v1",
            "status": "RESEARCH_COMPLETE",
            "plan_id": plan_id,
            "dataset_id": dataset_id,
            "model_hash": model_hash,
            "purpose": plan.purpose,
            "registered_at": registration["registered_at"],
            "completed_at": datetime.now(UTC).isoformat(),
            "implementation_hash": registration["implementation_hash"],
            "split_rows": {k: len(v) for k, v in splits.items()},
            "purged_rows": len(data["rows"]) - sum(len(x) for x in splits.values()),
            "calibration": "sigmoid_on_separate_chronological_window",
            "metrics": _metrics(splits["test"], probabilities, plan.probability_threshold),
            "development_validation": cpcv,
            "test_predictions_sha256": digest(test_predictions),
            "validation_scope": "frozen_holdout_with_optional_development_CPCV_family_deflation_separate_no_promotion",
            "can_trade": False,
            "can_promote": False,
        }
        write_once(
            attempt / "test_predictions.json",
            test_predictions,
        )
        write_once(attempt / "result.json", result)
        return result
    except Exception as exc:
        write_once(
            attempt / "failed.json", {"status": "FAILED", "reason": str(exc), "can_trade": False}
        )
        raise


def hashlib_file(path: Path) -> str:
    import hashlib

    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _logit(probabilities: np.ndarray) -> np.ndarray:
    p = np.clip(probabilities, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p)).reshape(-1, 1)


def predict_report(root: Path, plan_id: str, feature: dict, envelope_raw: dict) -> dict:
    """Fresh report-only prediction. Persist before any future outcome join.

    Loads only the locally produced, hash-checked research artifact. Never
    expose this function as a public request with caller-supplied pickle paths.
    """
    import joblib

    plan, _ = load_plan(root, plan_id)
    directory = root / "runs" / plan_id
    result = read_object(directory / "result.json")
    path = directory / "research_model.joblib"
    if path.is_symlink() or hashlib_file(path) != result["model_hash"]:
        raise ValueError("model_hash_mismatch")
    envelope = DecisionEnvelope.from_dict(envelope_raw)
    if (
        feature.get("v") != 2
        or feature.get("decision") != "fired"
        or timestamp(feature["bar_ts"]) != envelope.bar_open
    ):
        raise ValueError("forward_feature_contract_mismatch")
    now = datetime.now(UTC)
    close = envelope.permission_snapshot.decision_bar.close_time
    if (
        not timestamp(result["completed_at"])
        < close
        <= timestamp(feature["captured_at"])
        <= timestamp(feature["ts"])
        <= now
        or (now - close).total_seconds() > 120
    ):
        raise ValueError("not_a_fresh_post_model_decision")
    for k, v in {
        "strategy_id": envelope.strategy_id,
        "symbol": envelope.symbol,
        "timeframe": envelope.timeframe,
        "decision_id": envelope.decision_id,
        "decision_bar_hash": envelope.decision_bar_content_hash,
        "side": envelope.side,
        "exchange": plan.cohort["exchange"],
        "fingerprint": plan.cohort["feature_fingerprint"],
    }.items():
        if feature.get(k) != v:
            raise ValueError("forward_feature_identity_mismatch")
    if (
        any(
            plan.cohort[k] != getattr(envelope, k)
            for k in ("strategy_id", "symbol", "timeframe", "entry_clock")
        )
        or feature.get("backfill") is not False
    ):
        raise ValueError("forward_cohort_mismatch")
    values = [feature["features"][name] for name in plan.features]
    if set(plan.features) & set(
        unavailable_features(feature["features"], feature.get("optional_inputs", []))
    ):
        raise ValueError("selected_feature_input_unavailable")
    if any(type(v) not in (float, int) or not np.isfinite(v) for v in values):
        raise ValueError("incomplete_selected_features")
    model = joblib.load(path)
    if model["plan_id"] != plan_id or model["dataset_id"] != result["dataset_id"]:
        raise ValueError("model_metadata_mismatch")
    raw = model["model"].predict_proba_up(pd.DataFrame([values], columns=plan.features))
    probability = float(model["calibrator"].predict_proba(_logit(raw))[0, 1])
    record = {
        "schema": "ml_forward_report_v1",
        "decision_id": envelope.decision_id,
        "plan_id": plan_id,
        "cohort": plan.cohort,
        "model_hash": result["model_hash"],
        "feature_hash": digest(feature),
        "decision_at": close.isoformat(),
        "prediction_at": datetime.now(UTC).isoformat(),
        "probability": probability,
        "target": "net_positive",
        "role": "report_only_post_arm",
        "can_trade": False,
        "can_promote": False,
    }
    record["prediction_hash"] = digest(record)
    write_once(root / "predictions" / plan_id / f"{envelope.decision_id}.json", record)
    return record


def pipeline_summary(root: Path, lane_dir: Path | None = None) -> dict:
    """Bounded artifact projection; never fit or deserialize a model here."""
    report: dict = {
        "schema": "ml_lab_pipeline_v1",
        "plans": [],
        "datasets": [],
        "runs": [],
        "predictions": [],
        "family_results": [],
        "errors": [],
        "artifacts_partial": False,
        "can_trade": False,
        "can_promote": False,
    }
    for key, pattern in (
        ("plans", "plans/*.json"),
        ("datasets", "datasets/*.json"),
        ("runs", "runs/*/result.json"),
        ("predictions", "predictions/*/*.json"),
        ("family_results", "family_results/*.json"),
    ):
        paths = sorted(root.glob(pattern))
        report[f"{key}_discovered"] = len(paths)
        report["artifacts_partial"] |= len(paths) > 20
        for path in paths[-20:]:
            try:
                obj = read_object(path)
                if key == "family_results" and (
                    obj.get("family_id") != path.stem
                    or digest({k: v for k, v in obj.items() if k != "report_hash"})
                    != obj.get("report_hash")
                    or digest(read_object(root / "families" / path.name)) != path.stem
                ):
                    raise ValueError("family_report_unverified")
                if key == "plans" and (
                    digest(obj["plan"]) != obj["plan_id"] or obj["plan_id"] != path.stem
                ):
                    raise ValueError("plan_hash_mismatch")
                if key == "runs":
                    validate_id(obj["plan_id"])
                    registered = read_object(root / "plans" / f"{obj['plan_id']}.json")
                    if (
                        obj["status"] != "RESEARCH_COMPLETE"
                        or obj["plan_id"] != path.parent.name
                        or digest(registered["plan"]) != obj["plan_id"]
                        or obj["implementation_hash"] != registered["implementation_hash"]
                        or (path.parent / "research_model.joblib").is_symlink()
                        or hashlib_file(path.parent / "research_model.joblib") != obj["model_hash"]
                    ):
                        raise ValueError("run_artifact_unverified")
                if (
                    key == "predictions"
                    and digest({k: v for k, v in obj.items() if k != "prediction_hash"})
                    != obj["prediction_hash"]
                ):
                    raise ValueError("prediction_hash_mismatch")
                if key == "datasets":
                    if (
                        digest({k: v for k, v in obj.items() if k != "dataset_id"})
                        != obj["dataset_id"]
                    ):
                        raise ValueError("dataset_hash_mismatch")
                    obj = {k: v for k, v in obj.items() if k != "rows"} | {"rows": len(obj["rows"])}
                report[key].append({**obj, "can_trade": False, "can_promote": False})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                report["errors"].append({"file": path.name, "reason": str(exc)})
        report[f"{key}_total"] = len(report[key])  # verified records in this bounded page
    report["failed_attempts"] = len(list(root.glob("runs/*/failed.json")))
    report["incomplete_attempts"] = sum(
        not (p.parent / "result.json").exists() and not (p.parent / "failed.json").exists()
        for p in root.glob("runs/*/started.json")
    )
    if lane_dir is not None:
        labels: dict[str, list[dict]] = {}
        exclusions: Counter = Counter()
        paths = sorted(lane_dir.glob("*.journal.jsonl"))
        report["ledger_coverage_complete"] = lane_dir.is_dir() and len(paths) <= 64
        for path in paths[:64]:
            found = build_ledger_labels(
                path, path.with_name(path.name.replace(".journal.jsonl", ".fills.jsonl"))
            )
            exclusions.update(found["rejections"])
            report["ledger_coverage_complete"] &= found["state"] == "VERIFIED"
            for label in found["labels"]:
                labels.setdefault(label["decision_id"], []).append(label)
        unique = {key: values[0] for key, values in labels.items() if len(values) == 1}
        report["ledger_bound_paper_labels"] = len(unique)
        cohort_counts: Counter = Counter(
            digest({k: label.get(k) for k in COHORT_FIELDS if k != "feature_fingerprint"})
            for label in unique.values()
        )
        report["label_cohort_counts"] = dict(cohort_counts)
        report["label_floor_note"] = (
            "200 train + 50 calibration + 50 holdout per exact cohort; count is not eligibility"
        )
        report["ledger_exclusions"] = dict(exclusions)
        feedback = []
        # Projection only. A prediction at/after exit may never become forward evidence.
        for prediction in report["predictions"]:
            label = unique.get(prediction.get("decision_id"))
            if label is None:
                continue
            try:
                prediction_body = {k: v for k, v in prediction.items() if k != "prediction_hash"}
                if digest(prediction_body) != prediction["prediction_hash"]:
                    raise ValueError("prediction_hash_mismatch")
                if any(
                    label[k] != prediction["cohort"][k]
                    for k in COHORT_FIELDS
                    if k != "feature_fingerprint"
                ):
                    raise ValueError("feedback_cohort_mismatch")
                if timestamp(prediction["prediction_at"]) >= timestamp(label["exit_at"]):
                    raise ValueError("prediction_after_outcome")
                feedback.append(
                    {
                        "decision_id": label["decision_id"],
                        "plan_id": prediction["plan_id"],
                        "prediction_hash": prediction["prediction_hash"],
                        "label_hash": label["label_hash"],
                        "probability": prediction["probability"],
                        "target": label["target"],
                        "net_bps": label["net_bps"],
                        "role": "report_only_post_arm",
                    }
                )
            except (KeyError, ValueError, TypeError) as exc:
                report["errors"].append({"file": "forward_feedback", "reason": str(exc)})
        report["feedback"] = feedback
    return report
