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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeAlias

from vnedge.data.candles import Candle

CanonicalCandleKey: TypeAlias = tuple[str, str, str]


class CanonicalCandleRouterError(RuntimeError):
    """Base class for fail-closed router errors."""


class CanonicalCandleOrderError(CanonicalCandleRouterError):
    """A publisher attempted to move a canonical stream backwards."""


class CanonicalCandleConflictError(CanonicalCandleRouterError):
    """The same canonical identity was published with different contents."""


class CanonicalCandleOverflowError(CanonicalCandleRouterError):
    """A subscriber failed to keep up and lost one or more events."""


@dataclass(frozen=True, slots=True)
class CanonicalCandleEvent:
    exchange: str
    candle: Candle
    published_at: datetime

    def __post_init__(self) -> None:
        exchange = self.exchange.strip().lower()
        if not exchange:
            raise ValueError("canonical candle exchange must not be empty")
        if not self.candle.is_closed:
            raise ValueError("canonical router accepts closed candles only")
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("canonical event published_at must be timezone-aware")
        if self.published_at < self.candle.close_time:
            raise ValueError("canonical event cannot be published before candle close")
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "published_at", self.published_at.astimezone(UTC))

    @property
    def key(self) -> CanonicalCandleKey:
        return (self.exchange, self.candle.symbol, self.candle.timeframe)


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
    ) -> None:
        self._router = router
        self.key = key
        self._queue: asyncio.Queue[CanonicalCandleEvent] = asyncio.Queue(
            maxsize=max_queue
        )
        self._overflow_count = 0
        self._closed = False

    @property
    def overflow_count(self) -> int:
        return self._overflow_count

    @property
    def failed(self) -> bool:
        return self._overflow_count > 0

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def get(self) -> CanonicalCandleEvent:
        if self._closed:
            raise CanonicalCandleRouterError("canonical subscription is closed")
        if self.failed:
            raise CanonicalCandleOverflowError(
                f"canonical subscription overflowed for {self.key}; "
                "consumer state must be rebuilt before recovery"
            )
        return await self._queue.get()

    def get_nowait(self) -> CanonicalCandleEvent:
        if self._closed:
            raise CanonicalCandleRouterError("canonical subscription is closed")
        if self.failed:
            raise CanonicalCandleOverflowError(
                f"canonical subscription overflowed for {self.key}; "
                "consumer state must be rebuilt before recovery"
            )
        return self._queue.get_nowait()

    def reset_after_recovery(self) -> None:
        """Clear queued history after the owner rebuilt from durable truth."""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._overflow_count = 0

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
            return False
        return True


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
        return (normalized, symbol, timeframe)

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
            self, key, max_queue=queue_size
        )
        self._subscribers.setdefault(key, set()).add(subscription)
        return subscription

    def publisher(
        self,
        exchange: str,
        *,
        clock: Callable[[], datetime] | None = None,
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
                )
            )

        return _publish

    def publish(self, event: CanonicalCandleEvent) -> bool:
        """Publish one event; return ``False`` only for an exact duplicate."""
        previous = self._last.get(event.key)
        if previous is not None:
            if event.candle.open_time < previous.candle.open_time:
                self._out_of_order += 1
                raise CanonicalCandleOrderError(
                    f"canonical stream moved backwards for {event.key}: "
                    f"{event.candle.open_time.isoformat()} < "
                    f"{previous.candle.open_time.isoformat()}"
                )
            if event.candle.open_time == previous.candle.open_time:
                if event.candle == previous.candle:
                    self._duplicates += 1
                    return False
                self._conflicts += 1
                raise CanonicalCandleConflictError(
                    f"conflicting canonical candle for {event.key} at "
                    f"{event.candle.open_time.isoformat()}"
                )

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
