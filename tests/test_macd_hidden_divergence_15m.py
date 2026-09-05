from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from vnedge.data.swings import SwingKind
from vnedge.strategy.macd_hidden_divergence_15m import (
    MacdDivergenceSignalIntent,
    MacdHiddenDivergence15mV1,
    MacdSpec,
    classify_price_oscillator_divergence,
    macd_frame,
)
from vnedge.strategy.scanner_contracts import scanner_runtime_contract
from vnedge.strategy.strategy_registry import (
    RESEARCH_ONLY,
    SHADOW_OBSERVE,
    STRATEGIES,
)


def _candles(count: int = 180) -> pd.DataFrame:
    index = np.arange(count)
    close = (
        100.0
        + 0.03 * index
        + np.sin(index * 2.0 * np.pi / 18.0)
        + 0.15 * np.sin(index * 2.0 * np.pi / 5.0)
    )
    return pd.DataFrame(
        {
            "timestamp": [
                datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * offset)
                for offset in range(count)
            ],
            "open": close - 0.02,
            "high": close + 0.15,
            "low": close - 0.15,
            "close": close,
            "volume": 10.0,
            "quote_volume": close * 10.0,
            "trade_count": 20,
            "data_quality": "ok",
            "is_closed": True,
            "timeframe": "15m",
            "exchange": "delta_india",
            "symbol": "BTCUSD",
        }
    )


@pytest.mark.parametrize(
    ("swing_kind", "first_price", "second_price", "first_osc", "second_osc", "expected"),
    [
        (SwingKind.LOW, 100.0, 99.0, -2.0, -1.0, "regular_bull"),
        (SwingKind.LOW, 100.0, 101.0, -1.0, -2.0, "hidden_bull"),
        (SwingKind.HIGH, 100.0, 101.0, 2.0, 1.0, "regular_bear"),
        (SwingKind.HIGH, 100.0, 99.0, 1.0, 2.0, "hidden_bear"),
    ],
)
def test_four_divergence_inequalities_are_frozen(
    swing_kind: SwingKind,
    first_price: float,
    second_price: float,
    first_osc: float,
    second_osc: float,
    expected: str,
) -> None:
    assert (
        classify_price_oscillator_divergence(
            swing_kind=swing_kind,
            first_price=first_price,
            second_price=second_price,
            first_oscillator=first_osc,
            second_oscillator=second_osc,
        )
        == expected
    )


def test_macd_is_pandas_adjust_false_with_span_min_periods() -> None:
    close = pd.Series(np.linspace(100.0, 130.0, 50))
    spec = MacdSpec()
    actual = macd_frame(close, spec)
    fast = close.ewm(span=12, adjust=False, min_periods=12).mean()
    slow = close.ewm(span=26, adjust=False, min_periods=26).mean()
    expected_line = fast - slow
    expected_signal = expected_line.ewm(span=9, adjust=False, min_periods=9).mean()

    pd.testing.assert_series_equal(
        actual["mhd_macd_line"],
        expected_line,
        check_names=False,
    )
    pd.testing.assert_series_equal(
        actual["mhd_macd_signal"],
        expected_signal,
        check_names=False,
    )
    assert actual["mhd_macd_line"].first_valid_index() == 25
    assert actual["mhd_macd_hist"].first_valid_index() == 33


def test_hidden_divergence_fires_once_at_second_swing_confirmation() -> None:
    strategy = MacdHiddenDivergence15mV1()
    prepared = strategy.prepare(_candles())
    fired = prepared.index[prepared["mhd_fire"].eq(1.0)].tolist()
    assert fired

    index = int(fired[0])
    intent = strategy.signal(prepared, index)
    assert isinstance(intent, MacdDivergenceSignalIntent)
    assert intent.divergence_evidence is not None
    evidence = intent.divergence_evidence
    assert evidence.divergence_kind in {"hidden_bull", "hidden_bear"}
    assert evidence.entry_clock == "next_15m_open"
    assert evidence.second.confirmed_at == (
        prepared.iloc[index]["timestamp"] + pd.Timedelta(minutes=15)
    ).to_pydatetime()
    assert evidence.episode_id in intent.reason
    assert intent.permission_snapshot is not None
    assert intent.permission_snapshot.context_bars == ()
    assert intent.permission_snapshot.snapshot_id == evidence.snapshot_id

    # The same pair is not emitted again on later decision bars.
    assert strategy.signal(prepared, index + 1) is None
    assert evidence.episode_id not in set(
        prepared.iloc[index + 1 :]["mhd_episode_id"].dropna().astype(str)
    )


def test_bad_bar_inside_pivot_window_removes_the_episode() -> None:
    strategy = MacdHiddenDivergence15mV1()
    candles = _candles()
    prepared = strategy.prepare(candles)
    first_fire = int(prepared.index[prepared["mhd_fire"].eq(1.0)][0])
    intent = strategy.signal(prepared, first_fire)
    assert isinstance(intent, MacdDivergenceSignalIntent)
    assert intent.divergence_evidence is not None
    episode = intent.divergence_evidence.episode_id

    second_open = pd.Timestamp(intent.divergence_evidence.second.open_time)
    anchor_index = int(candles.index[candles["timestamp"].eq(second_open)][0])
    candles.loc[anchor_index + 1, "data_quality"] = "gap"
    quarantined = strategy.prepare(candles)
    assert episode not in set(quarantined["mhd_episode_id"].dropna().astype(str))


def test_forming_bar_is_rejected_before_swing_detection() -> None:
    candles = _candles(50)
    candles.loc[len(candles) - 1, "is_closed"] = False
    with pytest.raises(ValueError, match="closed candles"):
        MacdHiddenDivergence15mV1().prepare(candles)


def test_strategy_is_registered_as_isolated_research_next_open() -> None:
    strategy_id = MacdHiddenDivergence15mV1.strategy_id
    assert STRATEGIES[strategy_id] is MacdHiddenDivergence15mV1
    assert strategy_id in RESEARCH_ONLY
    assert strategy_id not in SHADOW_OBSERVE
    contract = scanner_runtime_contract(strategy_id)
    assert contract is not None
    assert contract.timeframe == "15m"
    assert contract.evidence_entry_clock == "next_15m_open"
    assert contract.context_timeframes == ()


def test_regular_divergence_is_measured_but_cannot_fire_hidden_id() -> None:
    strategy = MacdHiddenDivergence15mV1()
    prepared = strategy.prepare(_candles())
    regular = prepared[prepared["mhd_pattern"].isin(["regular_bull", "regular_bear"])]
    assert not regular.empty
    assert regular["mhd_fire"].eq(0.0).all()
    assert all(strategy.signal(prepared, int(index)) is None for index in regular.index)
