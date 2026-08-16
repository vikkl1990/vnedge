from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.ml.execution_cost_model import (
    ExecutionCostSample,
    ExecutionCostTrainingConfig,
    train_execution_cost_quantiles,
)
from vnedge.risk.fee_model import ExecutionCostFeatures

START = datetime(2026, 1, 1, tzinfo=UTC)


def feature(index: int) -> ExecutionCostFeatures:
    spread = Decimal(index % 20) / Decimal(4) + Decimal("0.5")
    return ExecutionCostFeatures(
        observed_at=START + timedelta(hours=index),
        venue="delta_india" if index % 2 else "binanceusdm",
        symbol="BTCUSDT" if index % 3 else "ETHUSDT",
        urgency="taker" if index % 4 else "maker",
        side="buy" if index % 2 else "sell",
        spread_bps=spread,
        book_imbalance=Decimal((index % 11) - 5) / Decimal(10),
        atr_1h_bps=Decimal(50 + index % 30),
        volume_rank_24h=Decimal(index % 100) / Decimal(100),
        size_notional_usd=Decimal(100 + index),
        data_quality="ok",
        hour_utc=index % 24,
        session="asia" if index % 24 < 8 else "eu_us",
    )


def sample(index: int) -> ExecutionCostSample:
    row = feature(index)
    # Deterministic relationship plus bounded time-varying residual.
    label = float(row.spread_bps * Decimal("1.7")) + (index % 5) * 0.1
    return ExecutionCostSample(row, label)


def test_quantile_training_is_chronological_embargoed_and_shadow_only() -> None:
    samples = [sample(index) for index in range(240)]
    artifact = train_execution_cost_quantiles(
        list(reversed(samples)),
        ExecutionCostTrainingConfig(
            min_samples=200,
            embargo_rows=3,
            min_samples_leaf=10,
            max_iter=80,
        ),
        trained_at=START + timedelta(days=20),
    )

    p50, p90 = artifact.predict_quantiles(feature(241))

    assert p90 >= p50
    assert artifact.report.train_rows + artifact.report.test_rows + 3 == 240
    assert artifact.report.capital_eligible is False
    assert artifact.report.can_trade is False
    assert artifact.report.can_promote is False
    assert artifact.model_id.startswith("execution_cost_hgbq_")


def test_training_refuses_thin_or_ambiguous_fill_evidence() -> None:
    with pytest.raises(ValueError, match="refusing to fit"):
        train_execution_cost_quantiles([sample(index) for index in range(199)])

    duplicated = [sample(index) for index in range(200)]
    duplicated[-1] = ExecutionCostSample(duplicated[-2].features, 2.0)
    with pytest.raises(ValueError, match="duplicate observed_at"):
        train_execution_cost_quantiles(duplicated)
