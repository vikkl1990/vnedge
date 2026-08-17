"""Deterministic L2 size-ahead queue model for maker research and paper replay.

The model uses only public level size and aggressor trades. It does not claim
exchange L3 priority, hidden-liquidity visibility, or capital readiness. Book
joins are pessimistically placed in front by default; reductions shrink the
visible queue ahead pro rata. Simulated orders are never added to the public
book size because replay snapshots do not contain our hypothetical order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class QueueSide(str, Enum):
    BID = "bid"
    ASK = "ask"


class JoinPolicy(str, Enum):
    FRONT = "front"
    BEHIND = "behind"
    PRO_RATA_FRONT = "pro_rata_front"


class CancelPolicy(str, Enum):
    PRO_RATA = "pro_rata"
    BACK = "back"


@dataclass(frozen=True, slots=True)
class QueueModelConfig:
    """Frozen policy set; changing it creates a distinct replay experiment."""

    join_policy: JoinPolicy = JoinPolicy.FRONT
    cancel_policy: CancelPolicy = CancelPolicy.PRO_RATA


@dataclass(slots=True)
class RestingOrder:
    order_id: str
    side: QueueSide
    price: float
    original_size: float
    remaining_size: float
    ts_insert_ms: int
    priority_key: tuple[int, int]
    queue_ahead: float

    @property
    def filled_size(self) -> float:
        return self.original_size - self.remaining_size

    @property
    def complete(self) -> bool:
        return self.remaining_size <= 1e-12


@dataclass(slots=True)
class LevelState:
    side: QueueSide
    price: float
    displayed_size: float
    front_ahead: float


@dataclass(frozen=True, slots=True)
class QueueFill:
    order_id: str
    side: QueueSide
    price: float
    quantity: float
    ts_ms: int
    complete: bool


def _positive(value: float, *, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _non_negative(value: float, *, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return number


class QueuePositionModel:
    """Price-time FIFO approximation using displayed size ahead.

    A trade only consumes the exact recorded level. A print through the level
    is not expanded into invented swept volume; this is intentionally
    pessimistic when the feed does not report every intervening execution.
    """

    def __init__(self, config: QueueModelConfig | None = None) -> None:
        self.config = config or QueueModelConfig()
        self.levels: dict[tuple[QueueSide, float], LevelState] = {}
        self.orders: dict[str, RestingOrder] = {}
        self._sequence = 0

    def on_book_level(self, side: QueueSide, price: float, size: float) -> None:
        level_price = _positive(price, label="queue level price")
        level_size = _non_negative(size, label="queue level size")
        key = (side, level_price)
        level = self.levels.get(key)
        if level is None:
            self.levels[key] = LevelState(side, level_price, level_size, level_size)
            self._refresh_queue_ahead(key)
            return

        old_size = level.displayed_size
        old_front = level.front_ahead
        if level_size > old_size:
            added = level_size - old_size
            if self.config.join_policy is JoinPolicy.FRONT:
                level.front_ahead += added
            elif self.config.join_policy is JoinPolicy.PRO_RATA_FRONT:
                level.front_ahead = (
                    level_size if old_size <= 0 else old_front * level_size / old_size
                )
        elif level_size < old_size:
            if self.config.cancel_policy is CancelPolicy.PRO_RATA:
                level.front_ahead = (
                    0.0 if old_size <= 0 else old_front * level_size / old_size
                )
            else:
                removed = old_size - level_size
                behind = max(0.0, old_size - old_front)
                level.front_ahead = max(0.0, old_front - max(0.0, removed - behind))
        level.displayed_size = level_size
        level.front_ahead = min(level.front_ahead, level_size)
        self._refresh_queue_ahead(key)

    def insert(
        self,
        order_id: str,
        side: QueueSide,
        price: float,
        size: float,
        ts_ms: int,
    ) -> RestingOrder:
        if not order_id.strip():
            raise ValueError("queue order_id must not be empty")
        if order_id in self.orders:
            raise ValueError(f"duplicate queue order_id: {order_id}")
        level_price = _positive(price, label="queue order price")
        order_size = _positive(size, label="queue order size")
        key = (side, level_price)
        if key not in self.levels:
            raise ValueError("cannot insert without an observed book level")
        self._sequence += 1
        order = RestingOrder(
            order_id=order_id,
            side=side,
            price=level_price,
            original_size=order_size,
            remaining_size=order_size,
            ts_insert_ms=int(ts_ms),
            priority_key=(int(ts_ms), self._sequence),
            queue_ahead=0.0,
        )
        self.orders[order_id] = order
        self._refresh_queue_ahead(key)
        return order

    def cancel(self, order_id: str) -> RestingOrder | None:
        order = self.orders.pop(order_id, None)
        if order is not None:
            self._refresh_queue_ahead((order.side, order.price))
        return order

    def modify(
        self,
        order_id: str,
        *,
        price: float,
        size: float,
        ts_ms: int,
    ) -> RestingOrder:
        previous = self.cancel(order_id)
        if previous is None:
            raise KeyError(order_id)
        return self.insert(order_id, previous.side, price, size, ts_ms)

    def on_trade(
        self,
        *,
        price: float,
        size: float,
        aggressor_buy: bool,
        ts_ms: int,
    ) -> tuple[QueueFill, ...]:
        trade_price = _positive(price, label="queue trade price")
        trade_size = _positive(size, label="queue trade size")
        side = QueueSide.ASK if aggressor_buy else QueueSide.BID
        key = (side, trade_price)
        level = self.levels.get(key)
        if level is None:
            return ()

        remaining = trade_size
        consumed_ahead = min(level.front_ahead, remaining)
        level.front_ahead -= consumed_ahead
        remaining -= consumed_ahead
        level.displayed_size = max(0.0, level.displayed_size - trade_size)

        fills: list[QueueFill] = []
        for order in self._orders_at(key):
            if remaining <= 1e-12:
                break
            quantity = min(order.remaining_size, remaining)
            if quantity <= 0:
                continue
            order.remaining_size -= quantity
            remaining -= quantity
            fills.append(
                QueueFill(
                    order_id=order.order_id,
                    side=order.side,
                    price=order.price,
                    quantity=quantity,
                    ts_ms=int(ts_ms),
                    complete=order.complete,
                )
            )
        self._refresh_queue_ahead(key)
        return tuple(fills)

    def _orders_at(self, key: tuple[QueueSide, float]) -> list[RestingOrder]:
        return sorted(
            (
                order
                for order in self.orders.values()
                if (order.side, order.price) == key and not order.complete
            ),
            key=lambda order: order.priority_key,
        )

    def _refresh_queue_ahead(self, key: tuple[QueueSide, float]) -> None:
        level = self.levels.get(key)
        if level is None:
            return
        ahead = level.front_ahead
        for order in self._orders_at(key):
            order.queue_ahead = ahead
            ahead += order.remaining_size


__all__ = [
    "CancelPolicy",
    "JoinPolicy",
    "LevelState",
    "QueueFill",
    "QueueModelConfig",
    "QueuePositionModel",
    "QueueSide",
    "RestingOrder",
]
