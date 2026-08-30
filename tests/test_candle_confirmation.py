from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.data.candles import Candle
from vnedge.runtime.candle_confirmation import (
    ClosedCandleConfirmationConfig,
    ClosedCandleConfirmationEngine,
)
from vnedge.strategy.realtime_entry import RealtimeEntryArm


def _arm() -> RealtimeEntryArm:
    return RealtimeEntryArm(
        episode_id=7,
        bar_index=10,
        long_level=101.0,
        short_level=99.0,
        atr=2.0,
        reference_price=100.0,
    )


def _candle(minute: int, close: str, *, closed: bool = True) -> Candle:
    open_time = datetime(2026, 8, 30, 12, minute, tzinfo=UTC)
    close_value = Decimal(close)
    return Candle(
        symbol="BTCUSD",
        timeframe="1m",
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open=Decimal(100),
        high=max(Decimal(102), close_value),
        low=min(Decimal(98), close_value),
        close=close_value,
        volume=Decimal(10),
        quote_volume=Decimal(1000),
        trade_count=2,
        is_closed=closed,
    )


def test_two_closed_1m_holds_create_candidate_but_not_same_close_fill():
    engine = ClosedCandleConfirmationEngine()
    engine.arm(
        symbol="BTC/USD:USD",
        arm=_arm(),
        armed_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert engine.on_closed_candle(_candle(0, "101.2")) is None
    candidate = engine.on_closed_candle(_candle(1, "101.4"))

    assert candidate is not None
    assert candidate.side == "long"
    assert candidate.confirmation_bars == 2
    assert candidate.decision_clock == "closed_1m"
    assert candidate.execution_clock == "next_1m_open"
    assert candidate.confirmation_close == 101.4
    assert not engine.active


def test_wick_beyond_level_does_not_confirm_when_close_is_inside():
    engine = ClosedCandleConfirmationEngine()
    engine.arm(
        symbol="BTCUSD",
        arm=_arm(),
        armed_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert engine.on_closed_candle(_candle(0, "101.2")) is None
    assert engine.on_closed_candle(_candle(1, "100.5")) is None
    assert engine.on_closed_candle(_candle(2, "101.3")) is None
    assert engine.stats()["consecutive_closes"] == 1
    assert engine.stats()["invalidated"] == 1


def test_short_confirmation_uses_closes_below_armed_level():
    engine = ClosedCandleConfirmationEngine()
    engine.arm(
        symbol="BTCUSD",
        arm=_arm(),
        armed_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert engine.on_closed_candle(_candle(0, "98.8")) is None
    candidate = engine.on_closed_candle(_candle(1, "98.6"))

    assert candidate is not None
    assert candidate.side == "short"
    assert candidate.level == 99.0


def test_forming_duplicate_and_wrong_symbol_fail_closed():
    engine = ClosedCandleConfirmationEngine()
    armed_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    engine.arm(symbol="BTCUSD", arm=_arm(), armed_at=armed_at)

    with pytest.raises(ValueError, match="forming"):
        engine.on_closed_candle(_candle(0, "101.2", closed=False))
    engine.on_closed_candle(_candle(0, "101.2"))
    with pytest.raises(ValueError, match="strictly ordered"):
        engine.on_closed_candle(_candle(0, "101.3"))

    engine.arm(symbol="BTCUSD", arm=_arm(), armed_at=armed_at)
    other = _candle(0, "101.2")
    other = Candle(
        symbol="ETHUSD",
        timeframe=other.timeframe,
        open_time=other.open_time,
        close_time=other.close_time,
        open=other.open,
        high=other.high,
        low=other.low,
        close=other.close,
        volume=other.volume,
        quote_volume=other.quote_volume,
        trade_count=other.trade_count,
    )
    with pytest.raises(ValueError, match="symbol"):
        engine.on_closed_candle(other)


def test_confirmation_expires_without_reusing_stale_arm():
    engine = ClosedCandleConfirmationEngine(
        ClosedCandleConfirmationConfig(required_closes=2, max_wait_bars=2)
    )
    engine.arm(
        symbol="BTCUSD",
        arm=_arm(),
        armed_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert engine.on_closed_candle(_candle(2, "101.5")) is None
    assert not engine.active
    assert engine.stats()["expired"] == 1
