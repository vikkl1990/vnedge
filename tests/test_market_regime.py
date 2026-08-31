from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pandas as pd

from vnedge.strategy.market_regime import (
    MarketRegimeConfig,
    MarketRegimeMachine,
    complete_weeks_from_daily,
    regime_from_closed,
)

TEST_CONFIG = MarketRegimeConfig(
    weekly_classifier="range_structure_v1",
    daily_location_bars=14,
    h4_ema_bars=5,
    min_complete_weeks=3,
    daily_ema_fast=2,
    daily_ema_slow=4,
    daily_ema_climate=7,
    macd_fast=2,
    macd_slow=4,
    macd_signal=2,
    rsi_period=3,
    rsi_discount=20,
    rsi_premium=90,
)


def _daily_weeks(specs: list[tuple[float, float, float]]) -> pd.DataFrame:
    start = datetime(2026, 7, 6, tzinfo=UTC)  # Monday
    rows = []
    daily_offsets = (-2.1, -2.0, -1.8, -1.4, -0.8, -1.3, 0.0)
    for week, (low, high, close) in enumerate(specs):
        for day in range(7):
            day_close = close + daily_offsets[day]
            rows.append(
                {
                    "timestamp": start + timedelta(days=week * 7 + day),
                    "open": day_close - 0.1,
                    "high": high,
                    "low": low,
                    "close": day_close,
                    "volume": 10.0,
                    "quote_volume": day_close * 10.0,
                    "is_closed": True,
                    "data_quality": "ok",
                }
            )
    return pd.DataFrame(rows)


def _h4(direction: str) -> pd.DataFrame:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    if direction == "up":
        closes = [80.0 + 0.2 * index + 0.02 * index**2 for index in range(30)]
    elif direction == "down":
        closes = [120.0 - 0.2 * index - 0.02 * index**2 for index in range(30)]
    else:
        closes = [100.0 + (0.1 if index % 2 else -0.1) for index in range(30)]
    return pd.DataFrame(
        [
            {
                "timestamp": start + timedelta(hours=4 * index),
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1.0,
                "quote_volume": close,
                "is_closed": True,
                "data_quality": "ok",
            }
            for index, close in enumerate(closes)
        ]
    )


def _shift(frame: pd.DataFrame, *, days: int) -> pd.DataFrame:
    shifted = frame.copy()
    shifted["timestamp"] = pd.to_datetime(shifted["timestamp"], utc=True) + pd.Timedelta(
        days=days
    )
    return shifted


def _flat_daily_weeks() -> pd.DataFrame:
    frame = _daily_weeks([(90, 110, 100), (90, 110, 100), (90, 110, 100)])
    frame["open"] = 100.0
    frame["close"] = 100.0
    frame["quote_volume"] = frame["volume"] * frame["close"]
    return frame


def test_complete_weeks_ignore_a_forming_partial_week() -> None:
    daily = _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)])
    partial = pd.DataFrame(
        [
            {
                "timestamp": datetime(2026, 7, 27, tzinfo=UTC),
                "open": 103,
                "high": 104,
                "low": 60,
                "close": 61,
                "volume": 1,
                "quote_volume": 61,
                "is_closed": True,
                "data_quality": "ok",
            }
        ]
    )
    weeks = complete_weeks_from_daily(pd.concat([daily, partial], ignore_index=True))
    assert len(weeks) == 3
    assert float(weeks.iloc[-1]["close"]) == 103.0


def test_aligned_weekly_daily_h4_allows_continuation_long() -> None:
    daily = _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)])
    regime = regime_from_closed(daily, _h4("up"), config=TEST_CONFIG)
    assert regime.ready
    assert (regime.weekly, regime.daily, regime.h4) == ("up", "mid", "up")
    assert regime.state == "continuation"
    assert regime.family == "continuation"
    assert regime.allow_long and not regime.allow_short
    assert regime.allows("long", family="continuation")
    assert regime.allows_scanner("htf", "long")
    assert not regime.allows_scanner("range", "long")
    assert regime.ema_state == "up"
    assert regime.macd_impulse == "on"
    assert regime.rsi_zone == "mid"
    assert regime.asof_bar is not None
    assert regime.asof_bar[0] in {"4h", "1d", "1w"}


def test_opposing_4h_fails_closed() -> None:
    daily = _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)])
    regime = regime_from_closed(daily, _h4("down"), config=TEST_CONFIG)
    assert regime.family == "flat"
    assert not regime.allow_long and not regime.allow_short
    assert regime.reason == "htf_invalidated:4h_opposes_weekly_up"
    assert regime.exit_reason == "htf_bias_invalidated"


def test_daily_rsi_premium_blocks_new_long_without_reversing_the_trend() -> None:
    daily = _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)])
    config = replace(TEST_CONFIG, rsi_premium=70)

    regime = regime_from_closed(daily, _h4("up"), config=config)

    assert regime.ready
    assert regime.ema_state == "up"
    assert regime.macd_impulse == "on"
    assert regime.rsi_zone == "premium"
    assert regime.state == "continuation"
    assert not regime.allow_long
    assert regime.reason == "daily_rsi_or_range_premium_no_chase"
    assert regime.exit_reason is None


def test_insufficient_closed_context_never_grants_permission() -> None:
    regime = regime_from_closed(
        _daily_weeks([(90, 100, 95)]),
        _h4("up").iloc[:5],
        config=TEST_CONFIG,
    )
    assert not regime.ready
    assert regime.family == "flat"
    assert not regime.allow_long and not regime.allow_short


def test_official_candle_quote_volume_does_not_satisfy_trade_lake_weekly_vwap() -> None:
    daily = _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)])
    config = replace(TEST_CONFIG, weekly_classifier="vwap_structure_v1")

    regime = regime_from_closed(daily, _h4("up"), config=config)

    assert not regime.ready
    assert regime.family == "flat"
    assert regime.reason.startswith("insufficient_closed_htf_context:")
    assert "weekly" in regime.reason


def test_vwap_classifier_accepts_only_complete_trade_lake_weekly_artifacts() -> None:
    daily = _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)])
    start = pd.Timestamp("2026-07-06", tz="UTC")
    artifacts = pd.DataFrame(
        [
            {
                "exchange": "delta_india",
                "symbol": "BTCUSD",
                "timeframe": "1w",
                "open_time": start + pd.Timedelta(days=7 * week),
                "close_time": start + pd.Timedelta(days=7 * (week + 1)),
                "vwap": value,
                "sum_base": 1.0,
                "sum_notional": value,
                "n_trades": 10,
                "source": "trade_lake",
                "coverage_ok": True,
            }
            for week, value in enumerate((95.0, 98.0, 100.0))
        ]
    )
    config = replace(TEST_CONFIG, weekly_classifier="vwap_structure_v1")

    regime = regime_from_closed(
        daily,
        _h4("up"),
        weekly_vwap_artifacts=artifacts,
        weekly_vwap_symbol="BTCUSD",
        config=config,
    )

    assert regime.ready
    assert regime.weekly == "up"


def test_range_structure_classifier_uses_official_ohlc_without_weekly_vwap() -> None:
    daily = _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)])
    daily = daily.drop(columns="quote_volume")
    config = replace(TEST_CONFIG, weekly_classifier="range_structure_v1")

    regime = regime_from_closed(daily, _h4("up"), config=config)

    assert regime.ready
    assert regime.weekly == "up"
    assert regime.state == "continuation"
    assert regime.allow_long


def test_range_state_selects_mean_revert_scanners_only() -> None:
    daily = _flat_daily_weeks()

    regime = regime_from_closed(daily, _h4("range"), config=TEST_CONFIG)

    assert regime.state == "mean_revert"
    assert regime.allows_scanner("range", "long")
    assert regime.allows_scanner("failed_break", "long")
    assert regime.allows_scanner("squeeze", "long")
    assert not regime.allows_scanner("htf", "long")
    assert not regime.allows_scanner("range", "short")


def test_state_machine_ignores_15m_calls_without_a_new_htf_close() -> None:
    daily = _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)])
    h4 = _h4("up")
    machine = MarketRegimeMachine(TEST_CONFIG)
    first = machine.step(daily, h4)

    repeated = machine.step(
        daily,
        h4,
        as_of=pd.Timestamp(first.as_of) + pd.Timedelta(minutes=15),
    )

    assert repeated is first
    assert repeated.state == "continuation"


def test_state_machine_marks_expansion_spent_on_closed_htf_range() -> None:
    machine = MarketRegimeMachine(TEST_CONFIG)
    trend = machine.step(
        _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)]),
        _h4("up"),
    )
    assert trend.state == "continuation"
    range_daily = _shift(
        _flat_daily_weeks(),
        days=28,
    )
    range_h4 = _shift(_h4("range"), days=28)

    spent = machine.step(range_daily, range_h4)

    assert spent.state == "mean_revert"
    assert spent.reason == "expansion_spent"
    assert spent.exit_reason == "htf_bias_invalidated"


def test_state_machine_marks_closed_range_break_as_expansion() -> None:
    machine = MarketRegimeMachine(TEST_CONFIG)
    ranged = machine.step(_flat_daily_weeks(), _h4("range"))
    assert ranged.state == "mean_revert"
    trend_daily = _shift(
        _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)]),
        days=28,
    )
    trend_h4 = _shift(_h4("up"), days=28)

    expansion = machine.step(trend_daily, trend_h4)

    assert expansion.state == "continuation"
    assert expansion.reason == "range_expansion"
    assert expansion.allow_long


def test_state_machine_closed_4h_flip_invalidates_continuation() -> None:
    machine = MarketRegimeMachine(TEST_CONFIG)
    machine.step(
        _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)]),
        _h4("up"),
    )
    later_daily = _shift(
        _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)]),
        days=28,
    )
    later_h4 = _shift(_h4("down"), days=28)

    invalidated = machine.step(later_daily, later_h4)

    assert invalidated.state == "flat"
    assert invalidated.reason == "htf_invalidated"
    assert invalidated.exit_reason == "htf_bias_invalidated"


def test_state_machine_data_fault_flattens_and_requests_exit() -> None:
    machine = MarketRegimeMachine(TEST_CONFIG)
    machine.step(
        _daily_weeks([(90, 100, 95), (92, 102, 98), (50, 200, 103)]),
        _h4("up"),
    )

    fault = machine.step(
        pd.DataFrame(),
        pd.DataFrame(),
        as_of=pd.Timestamp("2026-08-30T12:00:00Z"),
        data_healthy=False,
        health_reason="product_spec_drift",
    )

    assert fault.state == "flat"
    assert not fault.ready
    assert fault.reason == "data_unhealthy:product_spec_drift"
    assert fault.exit_reason == "htf_bias_invalidated"
