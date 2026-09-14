"""Event-time validation and registered-family deflation for the research Lab.

No promotion authority. CPCV is development evidence, never the final holdout.
Family statistics use every registered attempt and aligned daily net-bps sums;
these are NOT portfolio returns or an annualized Sharpe ratio.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import rankdata

from vnedge.ml.ledger_labels import digest, timestamp


def event_purged_splits(
    rows: list[dict], embargo_seconds: int
) -> list[tuple[list[int], list[int]]]:
    """Six groups/two test groups; purge full decision→availability intervals."""
    if len(rows) < 6 or embargo_seconds < 0:
        raise ValueError("insufficient_cpcv_rows_or_invalid_embargo")
    starts = [timestamp(r["decision_at"]) for r in rows]
    ends = [timestamp(r["available_at"]) for r in rows]
    if starts != sorted(starts) or any(a > b for a, b in zip(starts, ends)):
        raise ValueError("invalid_cpcv_chronology")
    groups = np.array_split(np.arange(len(rows)), 6)
    embargo = timedelta(seconds=embargo_seconds)
    output = []
    for combination in combinations(range(6), 2):
        test = sorted(int(i) for g in combination for i in groups[g])
        # Group envelopes conservatively include gaps between sparse decisions.
        intervals = [
            (starts[int(groups[g][0])], max(ends[int(i)] for i in groups[g])) for g in combination
        ]
        train = [
            i
            for i in range(len(rows))
            if i not in test
            and not any(
                starts[i] <= end + embargo and ends[i] + embargo >= start
                for start, end in intervals
            )
        ]
        output.append((train, test))
    return output


def development_validation(rows: list[dict], plan) -> dict:
    """Fixed CPCV classifier with separate calibration inside EVERY fold."""
    from sklearn.linear_model import LogisticRegression
    from threadpoolctl import threadpool_limits

    from vnedge.ml.lab_pipeline import _logit, _metrics
    from vnedge.ml.trainer import train_classifier

    development = [
        r
        for r in rows
        if timestamp(r["available_at"]) + timedelta(seconds=plan.embargo_seconds) < plan.test_start
    ]
    splits = event_purged_splits(development, plan.embargo_seconds)
    prepared = []
    for train, test in splits:
        cal_n = max(plan.min_calibration, len(train) // 5)
        calibration = train[-cal_n:]
        if not calibration:
            raise ValueError("cpcv_calibration_empty")
        boundary = timestamp(development[calibration[0]]["decision_at"])
        fit = [
            i
            for i in train[:-cal_n]
            if timestamp(development[i]["available_at"]) + timedelta(seconds=plan.embargo_seconds)
            < boundary
        ]
        if len(fit) < plan.min_train or len(calibration) < plan.min_calibration or len(test) < 30:
            raise ValueError("cpcv_fold_sample_floor")
        if any(
            len({development[i]["target"] for i in indexes}) < 2 for indexes in (fit, calibration)
        ):
            raise ValueError("cpcv_fold_single_class")
        prepared.append((fit, calibration, test))
    forecasts: dict[int, list[float]] = {}
    folds = []

    def matrix(indexes):
        return pd.DataFrame([development[i]["features"] for i in indexes], columns=plan.features)

    with threadpool_limits(limits=2):
        for fit, calibration, test in prepared:
            model = train_classifier(
                matrix(fit),
                pd.Series([development[i]["target"] for i in fit]),
                params={"early_stopping": False},
                compute_importances=False,
            )
            calibrator = LogisticRegression(random_state=7).fit(
                _logit(model.predict_proba_up(matrix(calibration))),
                [development[i]["target"] for i in calibration],
            )
            probabilities = calibrator.predict_proba(_logit(model.predict_proba_up(matrix(test))))[
                :, 1
            ]
            for i, p in zip(test, probabilities):
                forecasts.setdefault(i, []).append(float(p))
            folds.append(
                {
                    "fit": len(fit),
                    "calibration": len(calibration),
                    "test": len(test),
                    "split_sha256": digest(
                        [
                            [development[i]["decision_id"] for i in ix]
                            for ix in (fit, calibration, test)
                        ]
                    ),
                }
            )
    indexes = sorted(forecasts)
    return {
        "schema": "ml_development_cpcv_v1",
        "folds": folds,
        "unique_decisions": len(indexes),
        "repeated_predictions_are_not_new_labels": True,
        "metrics": _metrics(
            [development[i] for i in indexes],
            np.array([np.mean(forecasts[i]) for i in indexes]),
            plan.probability_threshold,
        ),
        "scope": "development_cross_validation_not_untouched_judgment",
    }


def register_family(root: Path, plan_ids: list[str]) -> str:
    from vnedge.ml.lab_pipeline import load_plan, write_once

    if not 2 <= len(plan_ids) <= 8 or len(set(plan_ids)) != len(plan_ids):
        raise ValueError("family_requires_2_to_8_unique_plans")
    plans = [load_plan(root, p)[0] for p in plan_ids]
    first = plans[0]
    now = datetime.now(UTC)
    for pid, plan in zip(plan_ids, plans):
        if plan.test_start <= now or (root / "runs" / pid).exists():
            raise ValueError("family_must_precede_runs_and_test_window")
        if plan.cohort != first.cohort or (plan.test_start, plan.test_end) != (
            first.test_start,
            first.test_end,
        ):
            raise ValueError("family_requires_same_cohort_and_holdout")
    body = {
        "plan_ids": sorted(plan_ids),
        "registered_at": now.isoformat(),
        "schema": "ml_validation_family_v1",
        "cohort": first.cohort,
        "test_start": first.test_start.isoformat(),
        "test_end": first.test_end.isoformat(),
    }
    family_id = digest(body)
    write_once(root / "families" / f"{family_id}.json", body)
    return family_id


def family_statistics(matrix: np.ndarray) -> dict:
    """Daily aligned evidence; tied OOS ranks use their average, never ordering."""
    from vnedge.ml.validation import deflated_sharpe_ratio

    values = np.asarray(matrix, dtype=float)
    if values.ndim != 2 or values.shape[0] < 32 or values.shape[1] < 2:
        raise ValueError("family_requires_32_days_and_2_trials")
    if not np.isfinite(values).all() or np.any(values.std(axis=0, ddof=1) == 0):
        raise ValueError("family_nonfinite_or_zero_variance")
    sharpes = values.mean(axis=0) / values.std(axis=0, ddof=1)
    blocks = np.array_split(np.arange(len(values)), 8)
    outcomes = []
    for selected in combinations(range(8), 4):
        inside = values[np.concatenate([blocks[i] for i in selected])]
        outside = values[np.concatenate([blocks[i] for i in range(8) if i not in selected])]

        def sr(v):
            std = v.std(axis=0, ddof=1)
            if np.any(std == 0):
                raise ValueError("family_partition_zero_variance")
            return v.mean(axis=0) / std

        ins, outs = sr(inside), sr(outside)
        winners = np.flatnonzero(np.isclose(ins, ins.max(), rtol=1e-12, atol=1e-12))
        ranks = rankdata(outs, method="average") / (values.shape[1] + 1)
        outcomes.append(
            float(
                np.mean([1 if ranks[i] < 0.5 else 0.5 if ranks[i] == 0.5 else 0 for i in winners])
            )
        )
    dsr = [
        float(deflated_sharpe_ratio(values[:, i], values.shape[1], trial_sharpes=sharpes))
        for i in range(values.shape[1])
    ]
    if not np.isfinite(dsr).all():
        raise ValueError("family_deflation_undefined")
    return {
        "pbo": float(np.mean(outcomes)),
        "dsr_by_trial": dsr,
        "raw_trials": values.shape[1],
        "calendar_days": values.shape[0],
        "basis": "daily_sum_unweighted_net_bps_not_portfolio_returns",
        "search_scope": "registered_family_only_not_all_historical_research",
    }


def validate_family(root: Path, family_id: str) -> dict:
    from vnedge.ml.lab_pipeline import (
        hashlib_file,
        load_plan,
        read_object,
        temporal_split,
        validate_id,
        write_once,
    )

    validate_id(family_id)
    family = read_object(root / "families" / f"{family_id}.json")
    if digest(family) != family_id:
        raise ValueError("family_hash_mismatch")
    columns, ids, labels_hash = [], None, None
    day_start, day_end = timestamp(family["test_start"]), timestamp(family["test_end"])
    # Full UTC days only: otherwise partial first/last days distort comparisons.
    if any(t.hour or t.minute or t.second or t.microsecond for t in (day_start, day_end)):
        raise ValueError("family_requires_full_utc_days")
    days = pd.date_range(day_start, day_end, freq="D", inclusive="left")
    for pid in family["plan_ids"]:
        plan, registration = load_plan(root, pid)
        attempt = root / "runs" / pid
        result = read_object(attempt / "result.json")  # missing/failed member blocks whole family
        started = read_object(attempt / "started.json")
        if (
            timestamp(started["started_at"]) < timestamp(family["registered_at"])
            or result["implementation_hash"] != registration["implementation_hash"]
            or result["model_hash"] != hashlib_file(attempt / "research_model.joblib")
            or result["plan_id"] != pid
            or result["status"] != "RESEARCH_COMPLETE"
        ):
            raise ValueError("family_run_unverified")
        dataset_id = result["dataset_id"]
        validate_id(dataset_id)
        dataset = read_object(root / "datasets" / f"{dataset_id}.json")
        if digest({k: v for k, v in dataset.items() if k != "dataset_id"}) != dataset_id:
            raise ValueError("family_dataset_hash_mismatch")
        rows = temporal_split(dataset["rows"], plan)["test"]
        row_ids = [r["decision_id"] for r in rows]
        current_hash = digest(
            [
                {
                    k: r[k]
                    for k in ("decision_id", "decision_at", "net_bps", "target", "available_at")
                }
                for r in rows
            ]
        )
        if ids is not None and (ids != row_ids or labels_hash != current_hash):
            raise ValueError("family_cannot_intersect_away_missing_outcomes")
        ids, labels_hash = row_ids, current_hash
        predictions = read_object(attempt / "test_predictions.json")
        if result.get("test_predictions_sha256") != digest(predictions):
            raise ValueError("family_predictions_unverified")
        ps = predictions["predictions"]
        if [p["decision_id"] for p in ps] != ids:
            raise ValueError("family_prediction_identity_mismatch")
        daily = {d.date(): 0.0 for d in days}
        for row, p in zip(rows, ps):
            probability = p["probability"]
            if type(probability) not in (int, float) or not 0 <= probability <= 1:
                raise ValueError("family_probability_invalid")
            daily[timestamp(row["decision_at"]).date()] += (
                row["net_bps"] if probability >= plan.probability_threshold else 0
            )
        columns.append(list(daily.values()))
    result = {
        "family_id": family_id,
        "plan_ids": family["plan_ids"],
        "label_panel_hash": labels_hash,
        "statistics": family_statistics(np.asarray(columns).T),
        "can_trade": False,
        "can_promote": False,
        "scope": "family_robustness_requires_separate_human_review_and_forward_execution_evidence",
    }
    result["report_hash"] = digest(result)
    write_once(root / "family_results" / f"{family_id}.json", result)
    return result
