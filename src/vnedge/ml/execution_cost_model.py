"""Research-only batch quantile model for execution-cost residuals.

Training is chronological and embargoed; there is no random shuffle.  The
artifact predicts P50/P90 round-trip execution cost, never exchange fees.  A
passing report only makes the artifact eligible for shadow comparison.  It
does not promote the model or make it capital-loadable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from vnedge.risk.fee_model import (
    FEATURE_SCHEMA_VERSION,
    ExecutionCostFeatures,
)


@dataclass(frozen=True, slots=True)
class ExecutionCostSample:
    features: ExecutionCostFeatures
    realized_exec_rt_bps: float

    def __post_init__(self) -> None:
        if not np.isfinite(self.realized_exec_rt_bps):
            raise ValueError("realized execution label must be finite")


@dataclass(frozen=True, slots=True)
class ExecutionCostTrainingConfig:
    min_samples: int = 200
    test_fraction: float = 0.20
    embargo_rows: int = 3
    min_p90_coverage: float = 0.85
    max_iter: int = 200
    max_depth: int = 4
    learning_rate: float = 0.05
    min_samples_leaf: int = 30
    l2_regularization: float = 1.0
    random_state: int = 7

    def __post_init__(self) -> None:
        if self.min_samples < 100:
            raise ValueError("execution-cost model requires at least 100 labels")
        if not 0.1 <= self.test_fraction <= 0.4:
            raise ValueError("test_fraction must be within [0.1, 0.4]")
        if self.embargo_rows < 0:
            raise ValueError("embargo_rows cannot be negative")
        if not 0.5 <= self.min_p90_coverage <= 1:
            raise ValueError("min_p90_coverage must be within [0.5, 1]")


@dataclass(frozen=True, slots=True)
class ExecutionCostOosReport:
    train_rows: int
    test_rows: int
    embargo_rows: int
    p50_mae_bps: float
    rules_median_mae_bps: float
    p90_coverage: float
    mean_predicted_p90_bps: float
    mean_realized_bps: float
    equal_or_better_accuracy: bool
    conservative_quantile: bool
    shadow_gate_passed: bool
    capital_eligible: bool = False
    can_trade: bool = False
    can_promote: bool = False


@dataclass(frozen=True)
class TrainedExecutionCostQuantileModel:
    """Serializable dual-quantile artifact implementing the fee-model protocol."""

    p50_model: object
    p90_model: object
    encoded_columns: tuple[str, ...]
    model_id: str
    trained_at: datetime
    feature_schema_version: str
    report: ExecutionCostOosReport
    runtime_approved: bool = False

    def predict_quantiles(self, features: ExecutionCostFeatures) -> tuple[float, float]:
        frame = _encode_rows([features], columns=self.encoded_columns)
        p50 = float(self.p50_model.predict(frame)[0])
        p90 = float(self.p90_model.predict(frame)[0])
        if not np.isfinite(p50) or not np.isfinite(p90):
            raise ValueError("execution-cost model returned a non-finite prediction")
        # Independent quantile fits can cross.  Runtime ordering is conservative,
        # while the report exposes calibration quality for human promotion.
        return p50, max(p50, p90)


def _encode_rows(
    features: Sequence[ExecutionCostFeatures],
    *,
    columns: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    raw = pd.DataFrame([feature.model_row() for feature in features])
    categorical = ["venue", "symbol", "urgency", "side", "session", "data_quality"]
    encoded = pd.get_dummies(raw, columns=categorical, dtype=float)
    if columns is None:
        return encoded.reindex(sorted(encoded.columns), axis=1)
    return encoded.reindex(columns=list(columns), fill_value=0.0)


def train_execution_cost_quantiles(
    samples: Sequence[ExecutionCostSample],
    config: ExecutionCostTrainingConfig | None = None,
    *,
    trained_at: datetime | None = None,
) -> TrainedExecutionCostQuantileModel:
    """Fit P50/P90 HistGB models and evaluate the untouched chronological tail."""
    from sklearn.ensemble import HistGradientBoostingRegressor

    cfg = config or ExecutionCostTrainingConfig()
    if len(samples) < cfg.min_samples:
        raise ValueError(
            f"only {len(samples)} fill labels; refusing to fit below {cfg.min_samples}"
        )
    ordered = sorted(samples, key=lambda sample: sample.features.observed_at)
    timestamps = [sample.features.observed_at for sample in ordered]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate observed_at values make the time split ambiguous")
    if any(sample.features.schema_version != FEATURE_SCHEMA_VERSION for sample in ordered):
        raise ValueError("mixed execution-cost feature schemas")

    all_x = _encode_rows([sample.features for sample in ordered])
    all_y = np.asarray([sample.realized_exec_rt_bps for sample in ordered], dtype=float)
    test_rows = max(1, round(len(ordered) * cfg.test_fraction))
    test_start = len(ordered) - test_rows
    train_end = test_start - cfg.embargo_rows
    if train_end < 100:
        raise ValueError("chronological split leaves fewer than 100 training labels")
    x_train, y_train = all_x.iloc[:train_end], all_y[:train_end]
    x_test, y_test = all_x.iloc[test_start:], all_y[test_start:]

    params = {
        "max_iter": cfg.max_iter,
        "max_depth": cfg.max_depth,
        "learning_rate": cfg.learning_rate,
        "min_samples_leaf": cfg.min_samples_leaf,
        "l2_regularization": cfg.l2_regularization,
        "random_state": cfg.random_state,
    }
    p50_model = HistGradientBoostingRegressor(loss="quantile", quantile=0.5, **params)
    p90_model = HistGradientBoostingRegressor(loss="quantile", quantile=0.9, **params)
    p50_model.fit(x_train, y_train)
    p90_model.fit(x_train, y_train)

    p50 = p50_model.predict(x_test)
    p90 = np.maximum(p50, p90_model.predict(x_test))
    baseline = float(np.median(y_train))
    p50_mae = float(np.mean(np.abs(y_test - p50)))
    baseline_mae = float(np.mean(np.abs(y_test - baseline)))
    coverage = float(np.mean(y_test <= p90))
    accuracy_ok = p50_mae <= baseline_mae
    conservative_ok = coverage >= cfg.min_p90_coverage
    report = ExecutionCostOosReport(
        train_rows=len(x_train),
        test_rows=len(x_test),
        embargo_rows=cfg.embargo_rows,
        p50_mae_bps=p50_mae,
        rules_median_mae_bps=baseline_mae,
        p90_coverage=coverage,
        mean_predicted_p90_bps=float(np.mean(p90)),
        mean_realized_bps=float(np.mean(y_test)),
        equal_or_better_accuracy=accuracy_ok,
        conservative_quantile=conservative_ok,
        shadow_gate_passed=accuracy_ok and conservative_ok,
    )
    trained = trained_at or datetime.now(UTC)
    if trained.tzinfo is None or trained.utcoffset() is None:
        raise ValueError("trained_at must be timezone-aware")
    identity = {
        "schema": FEATURE_SCHEMA_VERSION,
        "trained_through": timestamps[train_end - 1].isoformat(),
        "rows": len(x_train),
        "columns": list(all_x.columns),
        "params": params,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return TrainedExecutionCostQuantileModel(
        p50_model=p50_model,
        p90_model=p90_model,
        encoded_columns=tuple(all_x.columns),
        model_id=f"execution_cost_hgbq_{digest}",
        trained_at=trained.astimezone(UTC),
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        report=report,
    )


__all__ = [
    "ExecutionCostOosReport",
    "ExecutionCostSample",
    "ExecutionCostTrainingConfig",
    "TrainedExecutionCostQuantileModel",
    "train_execution_cost_quantiles",
]
