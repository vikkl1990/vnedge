"""Canonical backtest report exposes the full auditable path, not just KPIs."""

import pandas as pd
import pytest

from vnedge.backtest.backtester import BacktestConfig, BacktestResult, Trade
from vnedge.backtest.report import REPORT_SCHEMA, build_backtest_report


def _ts(hour: int) -> pd.Timestamp:
    return pd.Timestamp("2026-08-01", tz="UTC") + pd.Timedelta(hours=hour)


def test_report_contains_equity_drawdown_daily_monthly_and_complete_trades():
    trades = (
        Trade(
            side="long",
            quantity=1.0,
            entry_ts=_ts(0),
            entry_price=100.0,
            exit_ts=_ts(2),
            exit_price=110.0,
            exit_reason="take_profit",
            gross_pnl_usd=10.0,
            fees_usd=1.0,
            funding_usd=-0.2,
            entry_reason="test_long",
            mae_usd=-2.0,
            mfe_usd=12.0,
        ),
        Trade(
            side="short",
            quantity=1.0,
            entry_ts=_ts(24),
            entry_price=110.0,
            exit_ts=_ts(27),
            exit_price=115.0,
            exit_reason="stop",
            gross_pnl_usd=-5.0,
            fees_usd=1.0,
            funding_usd=0.1,
            entry_reason="test_short",
        ),
    )
    curve = pd.Series(
        [500.0, 508.8, 502.9],
        index=[_ts(0), _ts(2), _ts(27)],
    )
    result = BacktestResult(
        symbol="BTC/USDT:USDT",
        timeframe="1h",
        trades=trades,
        equity_curve=curve,
        skipped_by_sizing=0,
        final_equity_usd=502.9,
        config=BacktestConfig(initial_equity_usd=500.0),
        funding_included=True,
        funding_event_count=2,
    )

    report = build_backtest_report(
        result,
        run_id="run-1",
        strategy_id="strategy_v1",
        exchange="binanceusdm",
        data_source="test.parquet",
        bars=28,
        parameters={"lookback": 12},
    )

    assert report["schema"] == REPORT_SCHEMA
    assert report["run"]["run_id"] == "run-1"
    assert report["run"]["costs"]["funding_included"] is True
    assert report["run"]["costs"]["funding_event_count"] == 2
    assert report["run"]["costs"]["execution_round_trip_bps"] == pytest.approx(14.0)
    assert report["run"]["costs"]["gate_round_trip_bps"] == pytest.approx(14.0)
    assert report["overview"]["gross_profit_usd"] == pytest.approx(5.0)
    assert report["overview"]["net_profit_usd"] == pytest.approx(2.9)
    assert report["overview"]["total_cost_usd"] == pytest.approx(2.1)
    assert len(report["equity_curve"]) == 3
    assert len(report["daily"]) == 2
    assert len(report["monthly"]) == 1
    assert len(report["trades"]) == 2
    assert report["trades"][0]["hold_seconds"] == 7_200
    assert report["trades"][0]["gross_bps_on_entry_notional"] == pytest.approx(1_000)
    assert report["trades"][0]["execution_cost_bps_on_entry_notional"] == pytest.approx(100)
    assert report["trades"][0]["funding_bps_on_entry_notional"] == pytest.approx(-20)
    assert report["trades"][0]["net_execution_bps_on_entry_notional"] == pytest.approx(880)
    assert report["trades"][0]["net_bps_on_entry_notional"] == pytest.approx(880)
    assert report["trades"][0]["mae_bps_on_entry_notional"] == pytest.approx(-200)
    assert report["trades"][0]["mfe_bps_on_entry_notional"] == pytest.approx(1_200)
    assert report["governance"]["can_trade"] is False
    assert report["governance"]["can_promote"] is False
    assert any("UNDER_SAMPLED" in warning for warning in report["warnings"])


def test_report_does_not_claim_funding_when_no_history_was_supplied():
    curve = pd.Series([500.0, 500.0], index=[_ts(0), _ts(1)])
    result = BacktestResult(
        symbol="BTC/USDT:USDT",
        timeframe="1h",
        trades=(),
        equity_curve=curve,
        skipped_by_sizing=0,
        final_equity_usd=500.0,
        config=BacktestConfig(),
    )

    report = build_backtest_report(
        result,
        run_id="no-funding",
        strategy_id="strategy_v1",
        exchange="binanceusdm",
        data_source="test.parquet",
        bars=2,
    )

    assert report["run"]["costs"]["funding_included"] is False
    assert report["run"]["costs"]["funding_event_count"] == 0
    assert any("FUNDING_EXCLUDED" in warning for warning in report["warnings"])
