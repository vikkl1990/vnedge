"""Bounded in-process delivery for canonical closed-candle events.

The router is deliberately transport-only.  It does not construct candles,
read Parquet, or make strategy decisions.  Producers publish the exact
trade-derived :class:`~vnedge.data.candles.Candle`; scanner lanes subscribe by
``(exchange, symbol, timeframe)``.  Queue overflow and ordering conflicts are
explicit failures so a slow consumer cannot silently skip decision bars.

During the correction-spec dark rollout the durable Parquet path remains the
authoritative decision input.  This module supplies the typed, bounded seam
needed to compare the future in-memory path against that baseline before the
disk poll is removed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeAlias

from vnedge.data.candles import BarState, Candle, CandleParquetStore
from vnedge.data.symbols import canonical_symbol

CanonicalCandleKey: TypeAlias = tuple[str, str, str]


class CanonicalCandleRouterError(RuntimeError):
    """Base class for fail-closed router errors."""


class CanonicalCandleOrderError(CanonicalCandleRouterError):
    """A publisher attempted to move a canonical stream backwards."""


class CanonicalCandleConflictError(CanonicalCandleRouterError):
    """The same canonical identity was published with different contents."""


class CanonicalCandleOverflowError(CanonicalCandleRouterError):
    """A subscriber failed to keep up and lost one or more events."""


class CanonicalCandleDurabilityError(CanonicalCandleRouterError):
    """A routed candle was missing from, or conflicted with, durable truth."""


@dataclass(frozen=True, slots=True)
class CanonicalCandleEvent:
    exchange: str
    candle: Candle
    published_at: datetime
    state: BarState = BarState.CLOSED_IMMUTABLE
    bar_version: int = 1
    watermark_event_time: datetime | None = None
    reorder_bound_ms: int | None = None
    raw_trade_durable: bool | None = None
    late_trade_policy: Literal["reject"] = "reject"

    def __post_init__(self) -> None:
        exchange = self.exchange.strip().lower()
        if not exchange:
            raise ValueError("canonical candle exchange must not be empty")
        if not self.candle.is_closed:
            raise ValueError("canonical router accepts closed candles only")
        if self.state is not BarState.CLOSED_IMMUTABLE:
            raise ValueError("canonical router accepts immutable closes only")
        if self.bar_version != 1:
            raise ValueError("canonical candle bar_version must be 1")
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("canonical event published_at must be timezone-aware")
        if self.published_at < self.candle.close_time:
            raise ValueError("canonical event cannot be published before candle close")
        watermark = self.watermark_event_time or self.candle.close_time
        if watermark.tzinfo is None or watermark.utcoffset() is None:
            raise ValueError("canonical event watermark must be timezone-aware")
        if watermark < self.candle.close_time:
            raise ValueError("canonical event watermark cannot precede candle close")
        if self.reorder_bound_ms is not None and self.reorder_bound_ms < 0:
            raise ValueError("canonical event reorder bound must be non-negative")
        if self.late_trade_policy != "reject":
            raise ValueError("canonical event late-trade policy must be reject")
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "published_at", self.published_at.astimezone(UTC))
        object.__setattr__(self, "watermark_event_time", watermark.astimezone(UTC))

    @property
    def key(self) -> CanonicalCandleKey:
        return (
            self.exchange,
            canonical_symbol(self.candle.symbol),
            self.candle.timeframe,
        )


@dataclass(frozen=True, slots=True)
class CanonicalWarmupResult:
    """Durable history loaded after subscribing and through the watermark."""

    key: CanonicalCandleKey
    watermark_open_time: datetime | None
    candles: tuple[Candle, ...]
    queued_duplicates_dropped: int


@dataclass(frozen=True, slots=True)
class CanonicalDurableMatch:
    """One router event proven equal to its immutable Parquet row."""

    event: CanonicalCandleEvent
    candle: Candle
    wait_ms: float


@dataclass(frozen=True, slots=True)
class CanonicalCandleRouterSnapshot:
    published: int
    duplicates: int
    conflicts: int
    out_of_order: int
    subscriber_overflows: int
    active_subscribers: int
    last_published_at: datetime | None


class CanonicalCandleSubscription:
    """One bounded consumer queue.

    Once overflowed, ``get`` raises until the owner discards/rebuilds its local
    state and explicitly calls :meth:`reset_after_recovery`.  This prevents a
    scanner from treating the next available bar as contiguous after a loss.
    """

    def __init__(
        self,
        router: CanonicalCandleRouter,
        key: CanonicalCandleKey,
        *,
        max_queue: int,
        watermark: CanonicalCandleEvent | None,
    ) -> None:
        self._router = router
        self.key = key
        self._queue: asyncio.Queue[CanonicalCandleEvent] = asyncio.Queue(
            maxsize=max_queue
        )
        self._overflow_count = 0
        self._closed = False
        self._watermark = watermark
        self._failure: CanonicalCandleRouterError | None = None

    @property
    def overflow_count(self) -> int:
        return self._overflow_count

    @property
    def failed(self) -> bool:
        return self._failure is not None

    @property
    def failure_reason(self) -> str | None:
        return str(self._failure) if self._failure is not None else None

    def assert_healthy(self) -> None:
        if self._closed:
            raise CanonicalCandleRouterError("canonical subscription is closed")
        if self._failure is not None:
            raise self._failure

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def watermark(self) -> CanonicalCandleEvent | None:
        """Last event published atomically before this subscription existed."""
        return self._watermark

    async def get(self) -> CanonicalCandleEvent:
        if self._closed:
            raise CanonicalCandleRouterError("canonical subscription is closed")
        if self._failure is not None:
            raise self._failure
        return await self._queue.get()

    def get_nowait(self) -> CanonicalCandleEvent:
        if self._closed:
            raise CanonicalCandleRouterError("canonical subscription is closed")
        if self._failure is not None:
            raise self._failure
        return self._queue.get_nowait()

    def reset_after_recovery(self) -> None:
        """Clear queued history after the owner rebuilt from durable truth."""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._overflow_count = 0
        self._failure = None

    def drop_warmup_duplicates(self, through: datetime | None) -> int:
        """Drop queued bars already reconstructed from durable warm-up.

        Forward events remain in their original order.  The operation has no
        await point, so a publisher on the same event loop cannot interleave a
        new event between the drain and restore.
        """
        if self._closed:
            raise CanonicalCandleRouterError("canonical subscription is closed")
        if self._failure is not None:
            raise self._failure
        retained: list[CanonicalCandleEvent] = []
        dropped = 0
        while True:
            try:
                event = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if through is not None and event.candle.open_time <= through:
                dropped += 1
            else:
                retained.append(event)
        for event in retained:
            self._queue.put_nowait(event)
        return dropped

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._router._unsubscribe(self)

    def _put(self, event: CanonicalCandleEvent) -> bool:
        if self._closed or self.failed:
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._overflow_count += 1
            self._failure = CanonicalCandleOverflowError(
                f"canonical subscription overflowed for {self.key}; "
                "consumer state must be rebuilt before recovery"
            )
            return False
        return True

    def _fail(self, error: CanonicalCandleRouterError) -> None:
        if not self._closed and self._failure is None:
            self._failure = error


class CanonicalCandleRouter:
    """Fan out ordered canonical candles to bounded lane subscriptions."""

    def __init__(self, *, default_queue_size: int = 32) -> None:
        if default_queue_size <= 0:
            raise ValueError("default_queue_size must be positive")
        self.default_queue_size = default_queue_size
        self._subscribers: dict[
            CanonicalCandleKey, set[CanonicalCandleSubscription]
        ] = {}
        self._last: dict[CanonicalCandleKey, CanonicalCandleEvent] = {}
        self._published = 0
        self._duplicates = 0
        self._conflicts = 0
        self._out_of_order = 0
        self._subscriber_overflows = 0
        self._last_published_at: datetime | None = None

    @staticmethod
    def _key(exchange: str, symbol: str, timeframe: str) -> CanonicalCandleKey:
        normalized = exchange.strip().lower()
        if not normalized or not symbol.strip() or not timeframe.strip():
            raise ValueError("canonical subscription key parts must not be empty")
        return (normalized, canonical_symbol(symbol), timeframe.strip())

    def subscribe(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        *,
        max_queue: int | None = None,
    ) -> CanonicalCandleSubscription:
        queue_size = self.default_queue_size if max_queue is None else max_queue
        if queue_size <= 0:
            raise ValueError("max_queue must be positive")
        key = self._key(exchange, symbol, timeframe)
        subscription = CanonicalCandleSubscription(
            self,
            key,
            max_queue=queue_size,
            watermark=self._last.get(key),
        )
        self._subscribers.setdefault(key, set()).add(subscription)
        return subscription

    def publisher(
        self,
        exchange: str,
        *,
        clock: Callable[[], datetime] | None = None,
        raw_trade_durable: bool | None = None,
        reorder_bound_ms: int | None = None,
        late_trade_policy: Literal["reject"] = "reject",
    ) -> Callable[[Candle], None]:
        """Return a candle-pipeline subscriber bound to one venue.

        This adapter keeps candle construction independent of runtime routing
        and gives the recorder a one-line dark-rollout integration point.
        """
        normalized = exchange.strip().lower()
        if not normalized:
            raise ValueError("canonical publisher exchange must not be empty")
        event_clock = clock or (lambda: datetime.now(UTC))

        def _publish(candle: Candle) -> None:
            self.publish(
                CanonicalCandleEvent(
                    exchange=normalized,
                    candle=candle,
                    published_at=event_clock(),
                    watermark_event_time=candle.close_time,
                    reorder_bound_ms=reorder_bound_ms,
                    raw_trade_durable=raw_trade_durable,
                    late_trade_policy=late_trade_policy,
                )
            )

        return _publish

    def publish(self, event: CanonicalCandleEvent) -> bool:
        """Publish one event; return ``False`` only for an exact duplicate."""
        previous = self._last.get(event.key)
        if previous is not None:
            if event.candle.open_time < previous.candle.open_time:
                self._out_of_order += 1
                order_error = CanonicalCandleOrderError(
                    f"canonical stream moved backwards for {event.key}: "
                    f"{event.candle.open_time.isoformat()} < "
                    f"{previous.candle.open_time.isoformat()}"
                )
                self._fail_stream(event.key, order_error)
                raise order_error
            if event.candle.open_time == previous.candle.open_time:
                if event.candle == previous.candle:
                    self._duplicates += 1
                    return False
                self._conflicts += 1
                conflict_error = CanonicalCandleConflictError(
                    f"conflicting canonical candle for {event.key} at "
                    f"{event.candle.open_time.isoformat()}"
                )
                self._fail_stream(event.key, conflict_error)
                raise conflict_error

        self._last[event.key] = event
        self._published += 1
        self._last_published_at = event.published_at
        for subscriber in tuple(self._subscribers.get(event.key, ())):
            overflow_before = subscriber.overflow_count
            subscriber._put(event)
            if subscriber.overflow_count > overflow_before:
                self._subscriber_overflows += 1
        return True

    def snapshot(self) -> CanonicalCandleRouterSnapshot:
        return CanonicalCandleRouterSnapshot(
            published=self._published,
            duplicates=self._duplicates,
            conflicts=self._conflicts,
            out_of_order=self._out_of_order,
            subscriber_overflows=self._subscriber_overflows,
            active_subscribers=sum(len(items) for items in self._subscribers.values()),
            last_published_at=self._last_published_at,
        )

    def _unsubscribe(self, subscription: CanonicalCandleSubscription) -> None:
        subscribers = self._subscribers.get(subscription.key)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            self._subscribers.pop(subscription.key, None)

    def _fail_stream(
        self,
        key: CanonicalCandleKey,
        error: CanonicalCandleRouterError,
    ) -> None:
        for subscription in tuple(self._subscribers.get(key, ())):
            subscription._fail(error)


async def warm_subscription_from_store(
    subscription: CanonicalCandleSubscription,
    store: CandleParquetStore,
    *,
    not_before: datetime | None = None,
    durable_timeout_seconds: float = 8.0,
    poll_seconds: float = 0.05,
) -> CanonicalWarmupResult:
    """Subscribe-first warm-up without losing the handoff bar.

    The subscription already exists before this function reads Parquet.  Its
    captured watermark bounds durable history; events published during the
    read accumulate in the bounded queue.  A missing watermark, key mismatch,
    or queue overflow fails closed.
    """
    exchange, symbol, timeframe = subscription.key
    if store.exchange is not None and store.exchange.lower() != exchange:
        raise CanonicalCandleDurabilityError(
            f"store exchange {store.exchange!r} does not match subscription {exchange!r}"
        )
    if durable_timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("durable timeout and poll interval must be positive")
    if not_before is not None:
        if not_before.tzinfo is None or not_before.utcoffset() is None:
            raise ValueError("not_before must be timezone-aware")
        not_before = not_before.astimezone(UTC)

    watermark = subscription.watermark
    if watermark is not None:
        deadline = time.monotonic() + durable_timeout_seconds
        while True:
            subscription.assert_healthy()
            durable = await asyncio.to_thread(
                store.read_at,
                symbol,
                timeframe,
                watermark.candle.open_time,
            )
            if durable is not None:
                if durable != watermark.candle:
                    raise CanonicalCandleDurabilityError(
                        f"durable watermark conflicts for {subscription.key} at "
                        f"{watermark.candle.open_time.isoformat()}"
                    )
                break
            if time.monotonic() >= deadline:
                raise CanonicalCandleDurabilityError(
                    f"durable watermark missing for {subscription.key} at "
                    f"{watermark.candle.open_time.isoformat()}"
                )
            await asyncio.sleep(poll_seconds)

    rows = await asyncio.to_thread(store.read, symbol, timeframe)
    through = watermark.candle.open_time if watermark is not None else None
    history = tuple(
        candle
        for candle in rows
        if (through is None or candle.open_time <= through)
        and (not_before is None or candle.open_time >= not_before)
    )
    if any(
        (exchange, candle.symbol, candle.timeframe) != subscription.key
        for candle in history
    ):
        raise CanonicalCandleDurabilityError(
            f"durable history key mismatch for {subscription.key}"
        )
    cutoff = history[-1].open_time if history else through
    dropped = subscription.drop_warmup_duplicates(cutoff)
    return CanonicalWarmupResult(
        key=subscription.key,
        watermark_open_time=through,
        candles=history,
        queued_duplicates_dropped=dropped,
    )


async def next_durable_candle(
    subscription: CanonicalCandleSubscription,
    store: CandleParquetStore,
    *,
    timeout_seconds: float = 8.0,
    poll_seconds: float = 0.05,
) -> CanonicalDurableMatch:
    """Dark-rollout adapter: router clock, Parquet decision truth."""
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("timeout and poll interval must be positive")
    event = await subscription.get()
    started = time.perf_counter()
    deadline = time.monotonic() + timeout_seconds
    while True:
        durable = await asyncio.to_thread(
            store.read_at,
            event.candle.symbol,
            event.candle.timeframe,
            event.candle.open_time,
        )
        if durable is not None:
            if durable != event.candle:
                raise CanonicalCandleDurabilityError(
                    f"router/parquet mismatch for {event.key} at "
                    f"{event.candle.open_time.isoformat()}"
                )
            return CanonicalDurableMatch(
                event=event,
                candle=durable,
                wait_ms=(time.perf_counter() - started) * 1000.0,
            )
        if time.monotonic() >= deadline:
            raise CanonicalCandleDurabilityError(
                f"router candle missing from durable store for {event.key} at "
                f"{event.candle.open_time.isoformat()}"
            )
        await asyncio.sleep(poll_seconds)
