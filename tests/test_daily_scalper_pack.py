"""Daily scalper pack research and gating."""

import pandas as pd

from vnedge.research.daily_scalper_pack import (
    DailyScalperCandidate,
    parse_candidate,
    run_daily_scalper_research,
)
from vnedge.strategy.base_strategy import SignalIntent
from vnedge.strategy.daily_scalper_pack import DailyScalperPack


class FakeBase:
    warmup_bars = 1

    def prepare(self, candles):
        return candles.copy()

    def signal(self, df, index):
        return SignalIntent(
            side="long",
            stop_price=99.0,
            take_profit_price=103.0,
            reason="quant_signal_pack long structure_break score L/S=5.0/1.0",
        )


def context_row(*, trigger=True):
    return pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-07-06T00:00:00Z")],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [1000.0],
            "ctx_1h_bias_long": [True],
            "ctx_1h_bos_up": [False],
            "ctx_1h_choch_up": [False],
            "ctx_1h_atr_pct": [0.50],
            "ctx_4h_bias_long": [False],
            "ctx_4h_bos_up": [False],
            "ctx_4h_choch_up": [False],
            "ctx_4h_bias_short": [False],
            "ctx_4h_bos_down": [False],
            "trigger_1m_long": [trigger],
        }
    )


def test_daily_scalper_requires_context_and_trigger():
    strategy = DailyScalperPack()
    strategy._base = FakeBase()
    relaxed = DailyScalperPack(require_1m_trigger=False)
    relaxed._base = FakeBase()

    intent = strategy.signal(context_row(trigger=True), 0)
    blocked = strategy.signal(context_row(trigger=False), 0)
    diagnostic = relaxed.signal(context_row(trigger=False), 0)

    assert intent is not None
    assert intent.side == "long"
    assert "daily_scalper_pack" in intent.reason
    assert blocked is None
    assert diagnostic is not None


def test_daily_scalper_research_marks_missing_lanes_untestable(tmp_path):
    candidate = DailyScalperCandidate("binanceusdm", "DOGE/USDT:USDT", "order_block")

    report = run_daily_scalper_research(tmp_path, candidates=(candidate,))

    result = report["results"][0]
    assert result["verdict"] == "UNTESTABLE"
    assert "missing data lane" in result["reasons"][0]
    assert report["policy"]["can_trade"] is False
    assert report["summary"]["untestable"] == 1


def test_parse_candidate_accepts_optional_side_filter():
    candidate = parse_candidate("bybit|DOGE/USDT:USDT|structure_break|long")

    assert candidate.exchange == "bybit"
    assert candidate.symbol == "DOGE/USDT:USDT"
    assert candidate.family == "structure_break"
    assert candidate.allowed_sides == ("long",)
