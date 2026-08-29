"""Frozen market-event records at the canonical data-plane boundary.

Only public trades may create OHLC.  Executable BBO observations are a
separate acceptance/protection tape and must never be folded into candles.
The candle record itself lives in :mod:`vnedge.data.candles`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.data.symbols import canonical_symbol


def _utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_decimal(value: Decimal | float | str, *, label: str) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return result


@dataclass(frozen=True, slots=True)
class PublicTrade:
    """One deduplicable public print from the raw trade lake.

    ``exchange`` and ``symbol`` are logical fields even though Parquet stores
    them in partition names.  Derived quote/taker quantities are properties so
    the raw row cannot carry two disagreeing copies of the same fact.
    """

    exchange: str
    symbol: str
    trade_id: str
    timestamp: datetime
    price: Decimal
    amount: Decimal
    is_buyer_maker: bool | None = None

    def __post_init__(self) -> None:
        exchange = self.exchange.strip().lower()
        trade_id = self.trade_id.strip()
        if not exchange:
            raise ValueError("trade exchange must not be empty")
        if not trade_id:
            raise ValueError("venue trade_id must not be empty")
        if self.is_buyer_maker is not None and not isinstance(self.is_buyer_maker, bool):
            raise ValueError("is_buyer_maker must be bool or None")
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "trade_id", trade_id)
        object.__setattr__(self, "timestamp", _utc(self.timestamp, label="trade timestamp"))
        object.__setattr__(self, "price", _positive_decimal(self.price, label="trade price"))
        object.__setattr__(self, "amount", _positive_decimal(self.amount, label="trade amount"))

    @property
    def quote_notional(self) -> Decimal:
        return self.price * self.amount

    @property
    def taker_buy_volume(self) -> Decimal:
        return self.amount if self.is_buyer_maker is False else Decimal(0)

    def validate_clock(
        self,
        received_at: datetime,
        *,
        future_slack: timedelta = timedelta(seconds=5),
    ) -> None:
        """Reject prints implausibly ahead of the local receipt clock."""
        received = _utc(received_at, label="trade received_at")
        if future_slack < timedelta(0):
            raise ValueError("future_slack must be non-negative")
        if self.timestamp > received + future_slack:
            raise ValueError("trade timestamp is beyond the allowed future slack")

    def storage_row(self) -> dict[str, object]:
        """Return the payload stored below exchange/symbol lake partitions."""
        side = "buy" if self.is_buyer_maker is False else "sell" if self.is_buyer_maker else ""
        return {
            "ts_ms": int(self.timestamp.timestamp() * 1000),
            "price": float(self.price),
            "amount": float(self.amount),
            "side": side,
            "trade_id": self.trade_id,
        }


@dataclass(frozen=True, slots=True)
class LaneBBO:
    """Exact executable BBO observation consumed by one scanner lane."""

    exchange: str
    symbol: str
    lane_id: str
    bid: Decimal
    ask: Decimal
    ts: datetime
    received_ts: datetime
    sequence: int | str | None
    source: str
    overflow_drops: int
    captured_at_ms: int
    exchange_timestamped: bool = False
    capture_overflow_drops: int = 0

    def __post_init__(self) -> None:
        exchange = self.exchange.strip().lower()
        lane_id = self.lane_id.strip()
        source = self.source.strip()
        if not exchange or not lane_id or not source:
            raise ValueError("BBO exchange, lane_id, and source must not be empty")
        bid = _positive_decimal(self.bid, label="BBO bid")
        ask = _positive_decimal(self.ask, label="BBO ask")
        if ask < bid:
            raise ValueError("BBO ask must not be below bid")
        if self.sequence is not None and (
            isinstance(self.sequence, bool) or not isinstance(self.sequence, (int, str))
        ):
            raise ValueError("BBO sequence must be an integer, string, or None")
        if self.overflow_drops < 0 or self.capture_overflow_drops < 0:
            raise ValueError("BBO overflow counters must be non-negative")
        if self.captured_at_ms <= 0:
            raise ValueError("BBO captured_at_ms must be positive")
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        object.__setattr__(self, "lane_id", lane_id)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "ts", _utc(self.ts, label="BBO event timestamp"))
        object.__setattr__(
            self,
            "received_ts",
            _utc(self.received_ts, label="BBO receive timestamp"),
        )

    def storage_row(self) -> dict[str, object]:
        return {
            "ts_ms": int(self.ts.timestamp() * 1000),
            "received_ts_ms": int(self.received_ts.timestamp() * 1000),
            "bid": float(self.bid),
            "ask": float(self.ask),
            "sequence": "" if self.sequence is None else str(self.sequence),
            "source": self.source,
            "exchange_timestamped": self.exchange_timestamped,
            "overflow_drops": self.overflow_drops,
            "capture_overflow_drops": self.capture_overflow_drops,
            "captured_at_ms": self.captured_at_ms,
            "lane_id": self.lane_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
        }


__all__ = ["LaneBBO", "PublicTrade"]
