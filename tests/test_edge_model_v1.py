"""edge_model_v1 learns from opportunity rows without route authority."""

from datetime import UTC

import pandas as pd

from vnedge.research.edge_model_v1 import (
    EdgeModelConfig,
    _feature_columns,
    backtest_edge_model,
    build_opportunity_dataset,
)
from vnedge.research.execution_edge_router import OpportunityRoute


def route(i: int, *, edge_score: float, maker_net: float, taker_net: float | None = None):
    ts = pd.Timestamp("2026-07-01T00:00:00Z") + pd.Timedelta(minutes=15 * i)
    return OpportunityRoute(
        event_id=f"event-{i}",
        ts=ts.isoformat(),
        side="long" if i % 2 == 0 else "short",
        source_id="test",
        strategy_id="test_scanner_v1",
        action="MAKER",
        reason="maker baseline routed without edge forecast",
        selected_route="MAKER_ONLY",
        selected_net_bps=maker_net,
        selected_gross_bps=maker_net + 7.0,
        selected_cost_bps=7.0,
        maker_net_bps=maker_net,
        maker_gross_bps=maker_net + 7.0,
        maker_cost_bps=7.0,
        taker_net_bps=maker_net - 5.0 if taker_net is None else taker_net,
        taker_gross_bps=maker_net + 7.0,
        taker_cost_bps=12.0,
        maker_fill_probability=0.60,
        expected_edge_bps=None,
        outcome="target" if maker_net > 0 else "stop",
        mfe_bps=abs(maker_net) + 12.0,
        mae_bps=-12.0,
        risk_bps=20.0,
        metadata={
            "exchange": "test",
            "symbol": "ETH/USDT:USDT",
            "timeframe": "15m",
            "edge_score": edge_score,
            "reason": f"demo score={edge_score:.1f}; features=impulse",
        },
    )


def make_routes(n: int = 220):
    rows = []
    for i in range(n):
        good = i % 4 in {0, 1}
        rows.append(route(i, edge_score=1.0 if good else 0.0, maker_net=45.0 if good else -15.0))
    return tuple(rows)


def test_edge_model_improves_oos_by_selecting_stable_feature():
    report = backtest_edge_model(
        make_routes(),
        config=EdgeModelConfig(
            train_fraction=0.70,
            min_train_samples=100,
            min_test_samples=40,
            min_model_trades=20,
            min_predicted_net_bps=25.0,
            min_profit_factor=1.5,
        ),
    )
    summary = report["summary"]

    assert summary["verdict"] == "MODEL_PAPER_CANDIDATE"
    assert summary["can_trade"] is False
    assert summary["can_promote"] is False
    assert summary["model_avg_net_bps"] > summary["raw_avg_net_bps"]
    assert summary["improvement_bps"] >= 20.0
    assert summary["model_profit_factor"] == 999.0
    assert summary["model_trades"] >= 20
    assert report["policy"]["decision_uses_forward_truth"] is False


def test_feature_builder_excludes_forward_truth_columns():
    df = build_opportunity_dataset(make_routes(20))
    features = set(_feature_columns(df))

    assert "maker_net_bps" not in features
    assert "taker_net_bps" not in features
    assert "event_id" not in features
    assert "ts" not in features
    assert "meta_edge_score" in features
    assert "reason_score" in features
    assert "reason_feature_impulse" in features
    assert df["ts"].iloc[0].tzinfo == UTC


def test_edge_model_under_sampled_before_training():
    report = backtest_edge_model(
        make_routes(20),
        config=EdgeModelConfig(min_train_samples=100, min_test_samples=10),
    )

    assert report["summary"]["verdict"] == "UNDER_SAMPLED"
    assert report["summary"]["model_trades"] == 0
    assert "need >=" in report["summary"]["primary_blocker"]
