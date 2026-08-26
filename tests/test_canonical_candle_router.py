from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.data.candles import Candle
from vnedge.runtime.canonical_candle_router import (
    CanonicalCandleConflictError,
    CanonicalCandleEvent,
    CanonicalCandleOrderError,
    CanonicalCandleOverflowError,
    CanonicalCandleRouter,
    CanonicalCandleRouterError,
)


def _candle(hour: int, *, close: str = "101", closed: bool = True) -> Candle:
    opened = datetime(2026, 8, 26, hour, tzinfo=UTC)
    return Candle(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=opened,
        close_time=opened + timedelta(hours=1),
        open=Decimal(100),
        high=Decimal(102),
        low=Decimal(99),
        close=Decimal(close),
        volume=Decimal(10),
        quote_volume=Decimal(1005),
        trade_count=2,
        is_closed=closed,
    )


def _event(hour: int, *, close: str = "101") -> CanonicalCandleEvent:
    return CanonicalCandleEvent(
        exchange="BINANCEUSDM",
        candle=_candle(hour, close=close),
        published_at=datetime(2026, 8, 26, hour + 1, tzinfo=UTC),
    )


def test_router_fans_out_only_to_exact_stream_key():
    router = CanonicalCandleRouter()
    first = router.subscribe("binanceusdm", "BTCUSDT", "1h")
    second = router.subscribe("BINANCEUSDM", "BTCUSDT", "1h")
    unrelated = router.subscribe("binanceusdm", "ETHUSDT", "1h")

    assert router.publish(_event(10)) is True
    assert first.get_nowait().candle.open_time.hour == 10
    assert second.get_nowait().candle.open_time.hour == 10
    assert unrelated.pending == 0
    assert router.snapshot().active_subscribers == 3


def test_exact_duplicate_is_idempotent_but_conflict_fails_closed():
    router = CanonicalCandleRouter()
    event = _event(10)

    assert router.publish(event) is True
    assert router.publish(event) is False
    with pytest.raises(CanonicalCandleConflictError):
        router.publish(_event(10, close="100.5"))

    snapshot = router.snapshot()
    assert snapshot.published == 1
    assert snapshot.duplicates == 1
    assert snapshot.conflicts == 1


def test_out_of_order_publish_fails_closed():
    router = CanonicalCandleRouter()
    router.publish(_event(11))

    with pytest.raises(CanonicalCandleOrderError):
        router.publish(_event(10))

    assert router.snapshot().out_of_order == 1


@pytest.mark.asyncio
async def test_slow_consumer_overflow_requires_explicit_recovery():
    router = CanonicalCandleRouter(default_queue_size=1)
    subscription = router.subscribe("binanceusdm", "BTCUSDT", "1h")

    router.publish(_event(10))
    router.publish(_event(11))
    router.publish(_event(12))

    assert subscription.failed is True
    assert router.snapshot().subscriber_overflows == 1
    with pytest.raises(CanonicalCandleOverflowError):
        await subscription.get()

    subscription.reset_after_recovery()
    assert subscription.failed is False
    router.publish(_event(13))
    assert (await subscription.get()).candle.open_time.hour == 13


def test_closed_only_and_timezone_aware_contracts():
    with pytest.raises(ValueError, match="closed candles only"):
        CanonicalCandleEvent(
            exchange="binanceusdm",
            candle=_candle(10, closed=False),
            published_at=datetime(2026, 8, 26, 11, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        CanonicalCandleEvent(
            exchange="binanceusdm",
            candle=_candle(10),
            published_at=datetime(2026, 8, 26, 11, tzinfo=UTC).replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="before candle close"):
        CanonicalCandleEvent(
            exchange="binanceusdm",
            candle=_candle(10),
            published_at=datetime(2026, 8, 26, 10, 59, tzinfo=UTC),
        )


def test_subscription_close_is_idempotent_and_stops_delivery():
    router = CanonicalCandleRouter()
    subscription = router.subscribe("binanceusdm", "BTCUSDT", "1h")

    subscription.close()
    subscription.close()
    router.publish(_event(10))

    assert router.snapshot().active_subscribers == 0
    with pytest.raises(CanonicalCandleRouterError, match="closed"):
        subscription.get_nowait()


def test_pipeline_publisher_adapter_normalizes_exchange_and_clock():
    router = CanonicalCandleRouter()
    subscription = router.subscribe("binanceusdm", "BTCUSDT", "1h")
    published_at = datetime(2026, 8, 26, 12, tzinfo=UTC)

    router.publisher("BINANCEUSDM", clock=lambda: published_at)(_candle(10))

    event = subscription.get_nowait()
    assert event.exchange == "binanceusdm"
    assert event.published_at == published_at
