"""Pine-parity replay tests for VNEDGE Algo ML Pro."""

import pytest
import pandas as pd

from vnedge.research.vnedge_algo_ml_pro_pine_replay import (
    PineReplayConfig,
    replay_prepared_vnedge_algo_ml_pro,
    summarize_pine_replay_trades,
)
from vnedge.strategy.vnedge_algo_ml_pro import VNEDGEAlgoMLProParams


def _prepared(rows: list[dict]) -> pd.DataFrame:
    base = pd.Timestamp("2026-07-21T00:00:00Z")
    out = pd.DataFrame(rows)
    out["timestamp"] = [base + pd.Timedelta(minutes=5 * i) for i in range(len(out))]
    out["volume"] = 100.0
    out["atr_base"] = out["atr_value"]
    if "confirmed_long" not in out:
        out["confirmed_long"] = False
    if "confirmed_short" not in out:
        out["confirmed_short"] = False
    out["confirmed_long"] = out["confirmed_long"].fillna(False)
    out["confirmed_short"] = out["confirmed_short"].fillna(False)
    return out


def test_pine_replay_enters_on_signal_close_and_stop_is_close_based():
    df = _prepared(
        [
            {
                "open": 99.0,
                "high": 100.0,
                "low": 98.5,
                "close": 100.0,
                "atr_value": 1.0,
                "st_band": 98.0,
                "confirmed_long": True,
            },
            {
                "open": 100.0,
                "high": 102.1,
                "low": 97.0,
                "close": 100.5,
                "atr_value": 1.0,
                "st_band": 99.0,
            },
            {
                "open": 100.5,
                "high": 106.1,
                "low": 100.0,
                "close": 104.0,
                "atr_value": 1.0,
                "st_band": 101.0,
            },
        ]
    )
    config = PineReplayConfig(fee_cost_bps=12.0, mark_open_at_end=False)

    trades = replay_prepared_vnedge_algo_ml_pro(
        df,
        params=VNEDGEAlgoMLProParams(use_mtf=False),
        config=config,
        fee_cost_bps=12.0,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.entry_index == 0
    assert trade.entry_price == 100.0
    assert trade.exit_reason == "TP3"
    assert trade.exit_price == 106.0
    assert trade.tp1_hit is True
    assert trade.tp3_hit is True
    assert trade.gross_bps == pytest.approx(600.0)
    assert trade.fee_aware_net_bps == pytest.approx(588.0)
    assert trade.paper_fee_aware_usd == pytest.approx(147.0)


def test_pine_replay_reverses_at_current_signal_close():
    df = _prepared(
        [
            {
                "open": 99.5,
                "high": 100.2,
                "low": 98.8,
                "close": 100.0,
                "atr_value": 1.0,
                "st_band": 98.0,
                "confirmed_long": True,
            },
            {
                "open": 100.0,
                "high": 101.5,
                "low": 99.5,
                "close": 101.0,
                "atr_value": 1.0,
                "st_band": 103.0,
                "confirmed_short": True,
            },
        ]
    )

    trades = replay_prepared_vnedge_algo_ml_pro(
        df,
        params=VNEDGEAlgoMLProParams(use_mtf=False),
        config=PineReplayConfig(mark_open_at_end=False),
        fee_cost_bps=10.0,
    )

    assert len(trades) == 1
    assert trades[0].exit_reason == "REVERSE"
    assert trades[0].exit_price == 101.0
    assert trades[0].gross_bps == pytest.approx(100.0)


def test_pine_replay_summary_keeps_visual_and_fee_aware_results_separate():
    df = _prepared(
        [
            {
                "open": 99.0,
                "high": 100.0,
                "low": 98.0,
                "close": 100.0,
                "atr_value": 1.0,
                "st_band": 98.0,
                "confirmed_long": True,
            },
            {
                "open": 100.0,
                "high": 106.2,
                "low": 99.0,
                "close": 103.0,
                "atr_value": 1.0,
                "st_band": 101.0,
            },
        ]
    )
    config = PineReplayConfig(fee_cost_bps=12.0, mark_open_at_end=False)
    trades = replay_prepared_vnedge_algo_ml_pro(
        df,
        params=VNEDGEAlgoMLProParams(use_mtf=False),
        config=config,
        fee_cost_bps=12.0,
    )

    summary = summarize_pine_replay_trades(trades, config=config)

    assert summary["visual_avg_bps"] == pytest.approx(600.0)
    assert summary["fee_aware_avg_bps"] == pytest.approx(588.0)
    assert summary["visual_paper_usd"] == pytest.approx(150.0)
    assert summary["fee_aware_paper_usd"] == pytest.approx(147.0)
    assert summary["promotion_gate"]["passed"] is False
