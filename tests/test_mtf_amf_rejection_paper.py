"""Paper-only execution wrapper for the MTF/AMF research scanner."""

from datetime import UTC
from itertools import pairwise

import numpy as np
import pandas as pd

from vnedge.strategy.mtf_amf_rejection_paper import (
    MTF_AMF_REJECTION_PAPER_ID,
    MtfAmfRejectionPaperStrategy,
)
from vnedge.strategy.strategy_registry import get_strategy_class


def _candles(hours: int = 420) -> pd.DataFrame:
    timestamp = pd.date_range("2025-01-01", periods=hours, freq="1h", tz=UTC)
    close = 100.0 + np.sin(np.arange(hours) * 0.7)
    return pd.DataFrame(
        {
            "timestamp": timestamp,
            "open": close,
            "high": np.full(hours, 101.6),
            "low": np.full(hours, 98.4),
            "close": close,
            "volume": np.full(hours, 1_000.0),
        }
    )


def test_paper_strategy_emits_stopped_tp_ladder_intent():
    strategy = MtfAmfRejectionPaperStrategy()
    frame = strategy.prepare(_candles())

    intents = [
        (index, strategy.signal(frame, index))
        for index in range(len(frame))
        if strategy.signal(frame, index) is not None
    ]

    assert intents
    _, intent = intents[0]
    assert intent is not None
    assert intent.side in {"long", "short"}
    assert len(intent.take_profit_levels) == 3
    assert intent.take_profit_price == intent.take_profit_levels[-1]
    if intent.side == "long":
        assert intent.stop_price < frame.iloc[intents[0][0]]["close"]
        assert all(level > frame.iloc[intents[0][0]]["close"] for level in intent.take_profit_levels)
    else:
        assert intent.stop_price > frame.iloc[intents[0][0]]["close"]
        assert all(level < frame.iloc[intents[0][0]]["close"] for level in intent.take_profit_levels)
    assert "PAPER_ONLY" in intent.reason
    assert "exit=TP1_partial_BE_TP2_trail_max62" in intent.reason


def test_paper_strategy_matches_research_cooldown_spacing():
    strategy = MtfAmfRejectionPaperStrategy()
    frame = strategy.prepare(_candles())

    signal_positions = [
        index
        for index, side in enumerate(frame["mtf_amf_signal_side"])
        if side
    ]

    assert signal_positions
    assert all(
        right - left > strategy.config.cooldown_bars
        for left, right in pairwise(signal_positions)
    )


def test_paper_strategy_features_are_causal_on_append():
    strategy = MtfAmfRejectionPaperStrategy()
    full = _candles()
    cutoff = 300

    before = strategy.prepare(full.iloc[:cutoff].copy())
    after = strategy.prepare(full).iloc[:cutoff].reset_index(drop=True)

    columns = [
        "one_hour_high",
        "one_hour_low",
        "four_hour_high",
        "four_hour_low",
        "amf_histogram",
        "amf_regime",
        "mtf_amf_signal_side",
    ]
    pd.testing.assert_frame_equal(
        before[columns].reset_index(drop=True),
        after[columns],
        check_exact=False,
        rtol=1e-12,
    )


def test_paper_strategy_is_registered_but_marked_paper_only():
    cls = get_strategy_class(MTF_AMF_REJECTION_PAPER_ID)

    assert cls is MtfAmfRejectionPaperStrategy
    assert cls.paper_only is True
