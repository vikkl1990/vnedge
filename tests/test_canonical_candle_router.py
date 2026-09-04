from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.data.candles import BarState, Candle, CandleParquetStore
from vnedge.runtime.canonical_candle_router import (
    CanonicalCandleConflictError,
    CanonicalCandleDurabilityError,
    CanonicalCandleEvent,
    CanonicalCandleOrderError,
    CanonicalCandleOverflowError,
    CanonicalCandleRouter,
    CanonicalCandleRouterError,
    next_durable_candle,
    warm_subscription_from_store,
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
    subscription = router.subscribe("binanceusdm", "BTCUSDT", "1h")
    event = _event(10)

    assert router.publish(event) is True
    assert router.publish(event) is False
    with pytest.raises(CanonicalCandleConflictError):
        router.publish(_event(10, close="100.5"))

    snapshot = router.snapshot()
    assert snapshot.published == 1
    assert snapshot.duplicates == 1
    assert snapshot.conflicts == 1
    assert subscription.failed is True
    with pytest.raises(CanonicalCandleConflictError):
        subscription.get_nowait()
    subscription.reset_after_recovery()
    assert subscription.failed is False


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

    router.publisher(
        "BINANCEUSDM",
        clock=lambda: published_at,
        raw_trade_durable=True,
        reorder_bound_ms=250,
    )(_candle(10))

    event = subscription.get_nowait()
    assert event.exchange == "binanceusdm"
    assert event.published_at == published_at
    assert event.state is BarState.CLOSED_IMMUTABLE
    assert event.bar_version == 1
    assert event.watermark_event_time == _candle(10).close_time
    assert event.raw_trade_durable is True
    assert event.reorder_bound_ms == 250
    assert event.late_trade_policy == "reject"


def test_router_rejects_non_immutable_or_preclose_watermark_provenance():
    with pytest.raises(ValueError, match="immutable closes"):
        CanonicalCandleEvent(
            exchange="binanceusdm",
            candle=_candle(10),
            published_at=datetime(2026, 8, 26, 11, tzinfo=UTC),
            state=BarState.FORMING,
        )
    with pytest.raises(ValueError, match="watermark cannot precede"):
        CanonicalCandleEvent(
            exchange="binanceusdm",
            candle=_candle(10),
            published_at=datetime(2026, 8, 26, 11, tzinfo=UTC),
            watermark_event_time=datetime(2026, 8, 26, 10, 59, tzinfo=UTC),
        )


def test_router_symbol_key_is_canonical_across_venue_and_storage_forms():
    router = CanonicalCandleRouter()
    subscription = router.subscribe("binanceusdm", "BTC/USDT:USDT", "1h")

    router.publish(_event(10))

    assert subscription.key == ("binanceusdm", "BTCUSDT", "1h")
    assert subscription.get_nowait().candle.symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_subscribe_first_warmup_uses_watermark_and_keeps_forward_bar(tmp_path):
    store = CandleParquetStore(tmp_path, exchange="BINANCEUSDM")
    store.upsert((_candle(9), _candle(10)))
    router = CanonicalCandleRouter()
    router.publish(_event(10))
    subscription = router.subscribe("binanceusdm", "BTC/USDT:USDT", "1h")
    store.upsert((_candle(11),))
    router.publish(_event(11))

    result = await warm_subscription_from_store(subscription, store)

    assert [c.open_time.hour for c in result.candles] == [9, 10]
    assert result.watermark_open_time is not None
    assert result.watermark_open_time.hour == 10
    assert subscription.pending == 1
    assert (await subscription.get()).candle.open_time.hour == 11


@pytest.mark.asyncio
async def test_dark_consumer_requires_exact_durable_candle(tmp_path):
    store = CandleParquetStore(tmp_path, exchange="binanceusdm")
    router = CanonicalCandleRouter()
    subscription = router.subscribe("binanceusdm", "BTCUSDT", "1h")
    event = _event(10)
    store.upsert((event.candle,))
    router.publish(event)

    matched = await next_durable_candle(subscription, store)

    assert matched.candle == event.candle
    assert matched.wait_ms >= 0


@pytest.mark.asyncio
async def test_dark_consumer_fails_closed_on_parquet_conflict(tmp_path):
    store = CandleParquetStore(tmp_path, exchange="binanceusdm")
    router = CanonicalCandleRouter()
    subscription = router.subscribe("binanceusdm", "BTCUSDT", "1h")
    store.upsert((_candle(10, close="100.5"),))
    router.publish(_event(10))

    with pytest.raises(CanonicalCandleDurabilityError, match="mismatch"):
        await next_durable_candle(subscription, store)
