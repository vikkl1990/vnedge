"""Deterministic tick-to-candle construction and Parquet persistence.

The execution stack historically consumed exchange-produced OHLCV frames.  This
module adds a bottom-up, exchange-independent candle representation for live
measurement and offline research without changing that existing boundary.

MEASUREMENT/RESEARCH ONLY: this module cannot mark a strategy tradeable, grant
capital eligibility, emit an OrderIntent, or bypass any risk/CostGate policy.

Policy decisions are explicit:

* every timestamp is timezone-aware UTC and buckets align to the Unix epoch;
* no incomplete candle is published or persisted;
* empty buckets are skipped (OHLC is never invented by forward filling);
* higher-timeframe candles require complete, consecutive lower bars.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from vnedge.data.parquet_store import sanitize_symbol
from vnedge.data.vwap import vwap_from_sums

if TYPE_CHECKING:
    from pyarrow import Schema

logger = logging.getLogger(__name__)


TF_SECONDS: dict[str, int] = {
    "1s": 1,
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
}

CANDLE_STORAGE_COLUMNS = (
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trade_count",
    "taker_buy_volume",
    "vwap",
)

_DECIMAL_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_volume",
    "vwap",
)
_STORAGE_QUANTUM = Decimal("0.000000000000000001")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("candle timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _decimal(value: Decimal | int | str) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError("candle numeric values must be finite")
    return result


def _timeframe_seconds(timeframe: str) -> int:
    try:
        return TF_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported candle timeframe: {timeframe!r}") from exc


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Cross-process advisory lock around partition read/modify/replace."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def floor_time(timestamp: datetime, timeframe: str) -> datetime:
    """Return the UTC epoch-aligned start of ``timestamp``'s bucket."""
    timestamp = _utc(timestamp)
    seconds = _timeframe_seconds(timeframe)
    epoch_seconds = int(timestamp.timestamp())
    floored = epoch_seconds - (epoch_seconds % seconds)
    return datetime.fromtimestamp(floored, tz=UTC)


@dataclass(frozen=True, slots=True)
class Candle:
    """Canonical candle shared by every supported timeframe."""

    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_volume: Decimal = Decimal(0)
    vwap: Decimal | None = None
    is_closed: bool = True

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("candle symbol must not be empty")
        seconds = _timeframe_seconds(self.timeframe)
        open_time = _utc(self.open_time)
        close_time = _utc(self.close_time)
        if open_time != floor_time(open_time, self.timeframe):
            raise ValueError("candle open_time is not aligned to its timeframe")
        if close_time != open_time + timedelta(seconds=seconds):
            raise ValueError("candle close_time must be exactly one period after open_time")

        values = {name: _decimal(getattr(self, name)) for name in _DECIMAL_FIELDS[:-1]}
        vwap = None if self.vwap is None else _decimal(self.vwap)
        if values["open"] <= 0 or values["high"] <= 0:
            raise ValueError("candle prices must be positive")
        if values["low"] <= 0 or values["close"] <= 0:
            raise ValueError("candle prices must be positive")
        if values["high"] < max(values["open"], values["close"]):
            raise ValueError("candle high is below open or close")
        if values["low"] > min(values["open"], values["close"]):
            raise ValueError("candle low is above open or close")
        if values["high"] < values["low"]:
            raise ValueError("candle high is below low")
        for name in ("volume", "quote_volume", "taker_buy_volume"):
            if values[name] < 0:
                raise ValueError(f"candle {name} must be non-negative")
        if values["taker_buy_volume"] > values["volume"]:
            raise ValueError("taker_buy_volume cannot exceed total volume")
        if self.trade_count < 0:
            raise ValueError("trade_count must be non-negative")
        if values["volume"] > 0 and self.trade_count == 0:
            raise ValueError("non-zero candle volume requires at least one trade")
        if vwap is not None and vwap <= 0:
            raise ValueError("candle vwap must be positive")

        object.__setattr__(self, "open_time", open_time)
        object.__setattr__(self, "close_time", close_time)
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "vwap", vwap)

    @property
    def range_bps(self) -> Decimal:
        return (self.high - self.low) * Decimal(10000) / self.open

    @property
    def body_bps(self) -> Decimal:
        return abs(self.close - self.open) * Decimal(10000) / self.open

    @property
    def duration(self) -> timedelta:
        return self.close_time - self.open_time


@dataclass(frozen=True, slots=True)
class Trade:
    timestamp: datetime
    price: Decimal
    amount: Decimal
    is_buyer_maker: bool | None = None

    def __post_init__(self) -> None:
        timestamp = _utc(self.timestamp)
        price = _decimal(self.price)
        amount = _decimal(self.amount)
        if price <= 0:
            raise ValueError("trade price must be positive")
        if amount <= 0:
            raise ValueError("trade amount must be positive")
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True, slots=True)
class RejectedTrade:
    """Operator-visible record for a trade rejected by candle construction."""

    symbol: str
    timeframe: str
    timestamp: datetime
    price: str
    amount: str
    is_buyer_maker: bool | None
    reason: str


class JsonlTradeQuarantine:
    """Optional durable sink for rejected trades; one locked JSON object per line."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def __call__(self, rejected: RejectedTrade) -> None:
        payload = {
            "symbol": rejected.symbol,
            "timeframe": rejected.timeframe,
            "timestamp": rejected.timestamp.isoformat(),
            "price": rejected.price,
            "amount": rejected.amount,
            "is_buyer_maker": rejected.is_buyer_maker,
            "reason": rejected.reason,
        }
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        with _exclusive_lock(lock_path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())


@dataclass(slots=True)
class CandleBuilder:
    """Build one timeframe from ordered trades, skipping empty buckets."""

    symbol: str
    timeframe: str
    _candle: Candle | None = field(default=None, init=False, repr=False)
    _last_trade_time: datetime | None = field(default=None, init=False, repr=False)
    _closed_through: datetime | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("builder symbol must not be empty")
        _timeframe_seconds(self.timeframe)

    def on_trade(
        self,
        timestamp: datetime,
        price: Decimal | int | str,
        amount: Decimal | int | str,
        is_buyer_maker: bool | None = None,
    ) -> Candle | None:
        """Consume a trade and return the prior candle when its period rolls."""
        trade = Trade(timestamp, _decimal(price), _decimal(amount), is_buyer_maker)
        bucket = floor_time(trade.timestamp, self.timeframe)
        if self._last_trade_time is not None and trade.timestamp < self._last_trade_time:
            raise ValueError("trades must be ordered by timestamp")
        if self._closed_through is not None and bucket < self._closed_through:
            raise ValueError("trade belongs to an already-closed candle")
        closed: Candle | None = None

        if self._candle is not None:
            if trade.timestamp < self._candle.open_time:
                raise ValueError("out-of-order trade precedes the forming candle")
            if bucket > self._candle.open_time:
                closed = replace(self._candle, is_closed=True)
                self._closed_through = closed.close_time
                self._candle = None

        if self._candle is None:
            seconds = _timeframe_seconds(self.timeframe)
            quote_volume = trade.price * trade.amount
            self._candle = Candle(
                symbol=self.symbol,
                timeframe=self.timeframe,
                open_time=bucket,
                close_time=bucket + timedelta(seconds=seconds),
                open=trade.price,
                high=trade.price,
                low=trade.price,
                close=trade.price,
                volume=trade.amount,
                quote_volume=quote_volume,
                trade_count=1,
                taker_buy_volume=(trade.amount if trade.is_buyer_maker is False else Decimal(0)),
                vwap=vwap_from_sums(quote_volume, trade.amount),
                is_closed=False,
            )
            self._last_trade_time = trade.timestamp
            return closed

        candle = self._candle
        volume = candle.volume + trade.amount
        quote_volume = candle.quote_volume + (trade.price * trade.amount)
        taker_buy_volume = candle.taker_buy_volume
        if trade.is_buyer_maker is False:
            taker_buy_volume += trade.amount
        self._candle = Candle(
            symbol=candle.symbol,
            timeframe=candle.timeframe,
            open_time=candle.open_time,
            close_time=candle.close_time,
            open=candle.open,
            high=max(candle.high, trade.price),
            low=min(candle.low, trade.price),
            close=trade.price,
            volume=volume,
            quote_volume=quote_volume,
            trade_count=candle.trade_count + 1,
            taker_buy_volume=taker_buy_volume,
            vwap=vwap_from_sums(quote_volume, volume),
            is_closed=False,
        )
        self._last_trade_time = trade.timestamp
        return closed

    def close_if_elapsed(self, now: datetime) -> Candle | None:
        """Finalize last-trade OHLC when ``close_time <= now``.

        No empty candle or midpoint-derived OHLC is created. With no forming
        candle this is a no-op; quiet buckets after the last trade remain gaps.
        """
        now = _utc(now)
        if self._candle is None or now < self._candle.close_time:
            return None
        closed = replace(self._candle, is_closed=True)
        self._closed_through = closed.close_time
        self._candle = None
        return closed

    def forming(self) -> Candle | None:
        return self._candle

    @property
    def closed_through(self) -> datetime | None:
        """Exclusive boundary through which this builder finalized a candle."""
        return self._closed_through


def merge_candles(symbol: str, timeframe: str, parts: Sequence[Candle]) -> Candle:
    """Merge one complete target bucket of consecutive, closed lower bars."""
    if not parts:
        raise ValueError("cannot merge an empty candle sequence")
    target_seconds = _timeframe_seconds(timeframe)
    source_timeframe = parts[0].timeframe
    source_seconds = _timeframe_seconds(source_timeframe)
    if source_seconds >= target_seconds or target_seconds % source_seconds:
        raise ValueError("target timeframe must be an exact multiple of the source timeframe")
    expected_parts = target_seconds // source_seconds
    if len(parts) != expected_parts:
        raise ValueError(f"{timeframe} requires exactly {expected_parts} {source_timeframe} bars")
    if any(not part.is_closed for part in parts):
        raise ValueError("only closed candles may be merged")
    if any(part.symbol != symbol for part in parts):
        raise ValueError("all candle parts must match the requested symbol")
    if any(part.timeframe != source_timeframe for part in parts):
        raise ValueError("all candle parts must have the same source timeframe")

    target_open = floor_time(parts[0].open_time, timeframe)
    target_close = target_open + timedelta(seconds=target_seconds)
    if parts[0].open_time != target_open or parts[-1].close_time != target_close:
        raise ValueError("candle parts do not cover one aligned target bucket")
    for previous, current in pairwise(parts):
        if previous.close_time != current.open_time:
            raise ValueError("candle parts contain a gap or overlap")

    volume = sum((part.volume for part in parts), Decimal(0))
    quote_volume = sum((part.quote_volume for part in parts), Decimal(0))
    taker_buy_volume = sum((part.taker_buy_volume for part in parts), Decimal(0))
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=target_open,
        close_time=target_close,
        open=parts[0].open,
        high=max(part.high for part in parts),
        low=min(part.low for part in parts),
        close=parts[-1].close,
        volume=volume,
        quote_volume=quote_volume,
        trade_count=sum(part.trade_count for part in parts),
        taker_buy_volume=taker_buy_volume,
        vwap=vwap_from_sums(quote_volume, volume),
        is_closed=True,
    )


@dataclass(slots=True)
class CandleAggregator:
    """Streaming closed-bar aggregator with a strict skip-on-gap policy."""

    symbol: str
    source_timeframe: str
    target_timeframe: str
    _parts: list[Candle] = field(default_factory=list, init=False, repr=False)
    _bucket: datetime | None = field(default=None, init=False, repr=False)
    _last_source_open: datetime | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        source_seconds = _timeframe_seconds(self.source_timeframe)
        target_seconds = _timeframe_seconds(self.target_timeframe)
        if source_seconds >= target_seconds or target_seconds % source_seconds:
            raise ValueError("target timeframe must be an exact multiple of source timeframe")

    def on_candle(self, candle: Candle) -> Candle | None:
        if not candle.is_closed:
            raise ValueError("aggregators accept closed candles only")
        if candle.symbol != self.symbol or candle.timeframe != self.source_timeframe:
            raise ValueError("candle does not match aggregator symbol/timeframe")
        if self._last_source_open is not None and candle.open_time <= self._last_source_open:
            raise ValueError("source candles must be strictly ordered without duplicates")
        self._last_source_open = candle.open_time
        bucket = floor_time(candle.open_time, self.target_timeframe)
        if self._bucket is not None and bucket < self._bucket:
            raise ValueError("out-of-order candle precedes the active target bucket")
        if self._bucket is None or bucket > self._bucket:
            self._parts = []
            self._bucket = bucket
        self._parts.append(candle)

        target_close = bucket + timedelta(seconds=_timeframe_seconds(self.target_timeframe))
        if candle.close_time < target_close:
            return None
        parts = tuple(self._parts)
        self._parts = []
        self._bucket = None
        if candle.close_time > target_close:
            return None
        try:
            return merge_candles(self.symbol, self.target_timeframe, parts)
        except ValueError:
            # An incomplete/gapped bucket is deliberately omitted; no OHLC is invented.
            return None


def aggregate_candle_series(
    symbol: str,
    source_timeframe: str,
    target_timeframe: str,
    candles: Iterable[Candle],
) -> tuple[Candle, ...]:
    """Resample an ordered closed-bar series, omitting incomplete buckets."""
    aggregator = CandleAggregator(symbol, source_timeframe, target_timeframe)
    output = []
    for candle in candles:
        merged = aggregator.on_candle(candle)
        if merged is not None:
            output.append(merged)
    return tuple(output)


@dataclass(frozen=True, slots=True)
class CandleWriteResult:
    paths: tuple[Path, ...]
    rows_written: int


class CandleParquetStore:
    """Locked atomic store using daily intraday and monthly hourly files."""

    def __init__(self, root: Path | str, *, exchange: str | None = None) -> None:
        self.root = Path(root)
        self.exchange = exchange.strip() if exchange else None

    def partition_path(self, candle: Candle) -> Path:
        root = self.root
        if self.exchange:
            root = root / f"exchange={self.exchange}"
        directory = root / sanitize_symbol(candle.symbol) / candle.timeframe
        if candle.timeframe in {"1h", "4h"}:
            name = candle.open_time.strftime("%Y-%m.parquet")
        else:
            name = candle.open_time.strftime("%Y-%m-%d.parquet")
        return directory / name

    @staticmethod
    def _schema() -> Schema:
        import pyarrow as pa

        decimal = pa.decimal128(38, 18)
        return pa.schema(
            [
                pa.field("open_time", pa.timestamp("us", tz="UTC"), nullable=False),
                pa.field("close_time", pa.timestamp("us", tz="UTC"), nullable=False),
                *(pa.field(name, decimal, nullable=False) for name in _DECIMAL_FIELDS[:6]),
                pa.field("trade_count", pa.int64(), nullable=False),
                pa.field("taker_buy_volume", decimal, nullable=False),
                pa.field("vwap", decimal, nullable=True),
            ]
        )

    @staticmethod
    def _storage_decimal(value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return value.quantize(_STORAGE_QUANTUM, rounding=ROUND_HALF_EVEN)

    @classmethod
    def _frame(cls, candles: Sequence[Candle]) -> pd.DataFrame:
        rows = []
        for candle in candles:
            row: dict[str, object] = {
                "open_time": candle.open_time,
                "close_time": candle.close_time,
                "trade_count": candle.trade_count,
            }
            for name in _DECIMAL_FIELDS:
                row[name] = cls._storage_decimal(getattr(candle, name))
            rows.append(row)
        return pd.DataFrame(rows, columns=cls._schema().names)

    @classmethod
    def _write_atomic(cls, path: Path, frame: pd.DataFrame) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(frame, schema=cls._schema(), preserve_index=False)
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp)
        os.replace(tmp, path)

    def upsert(self, candles: Iterable[Candle]) -> CandleWriteResult:
        groups: dict[Path, list[Candle]] = {}
        for candle in candles:
            if not candle.is_closed:
                raise ValueError("forming candles cannot be persisted")
            groups.setdefault(self.partition_path(candle), []).append(candle)

        rows_written = 0
        for path, partition in groups.items():
            new_frame = self._frame(partition)
            lock_path = path.with_suffix(f"{path.suffix}.lock")
            with _exclusive_lock(lock_path):
                if path.exists():
                    existing = pd.read_parquet(path)
                    frame = pd.concat([existing, new_frame], ignore_index=True)
                else:
                    frame = new_frame
                frame = frame.drop_duplicates(subset="open_time", keep="last")
                frame = frame.sort_values("open_time").reset_index(drop=True)
                self._write_atomic(path, frame)
            rows_written += len(partition)
        return CandleWriteResult(tuple(sorted(groups)), rows_written)

    def read(self, symbol: str, timeframe: str) -> list[Candle]:
        _timeframe_seconds(timeframe)
        root = self.root
        if self.exchange:
            root = root / f"exchange={self.exchange}"
        directory = root / sanitize_symbol(symbol) / timeframe
        frames = [pd.read_parquet(path) for path in sorted(directory.glob("*.parquet"))]
        if not frames:
            return []
        frame = pd.concat(frames, ignore_index=True)
        frame = frame.drop_duplicates(subset="open_time", keep="last")
        frame = frame.sort_values("open_time")
        candles = []
        for row in frame.to_dict("records"):
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=row["open_time"].to_pydatetime(),
                    close_time=row["close_time"].to_pydatetime(),
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                    quote_volume=row["quote_volume"],
                    trade_count=int(row["trade_count"]),
                    taker_buy_volume=row["taker_buy_volume"],
                    vwap=row["vwap"],
                    is_closed=True,
                )
            )
        return candles


_AGGREGATION_CHAIN = (
    ("1s", "1m"),
    ("1m", "5m"),
    ("5m", "15m"),
    ("15m", "1h"),
    ("1h", "4h"),
)


class CandlePipeline:
    """Live one-symbol tick pipeline publishing closed base through 4h bars."""

    def __init__(
        self,
        symbol: str,
        *,
        base_timeframe: str = "1m",
        store: CandleParquetStore | None = None,
        subscribers: Iterable[Callable[[Candle], None]] = (),
        rejected_trade_sink: Callable[[RejectedTrade], None] | None = None,
    ) -> None:
        if base_timeframe not in {"1s", "1m"}:
            raise ValueError("live candle pipeline base_timeframe must be '1s' or '1m'")
        self.symbol = symbol
        self.builder = CandleBuilder(symbol, base_timeframe)
        self.store = store
        self.subscribers = tuple(subscribers)
        self.rejected_trade_sink = rejected_trade_sink
        start = 0 if base_timeframe == "1s" else 1
        self._aggregators = {
            source: CandleAggregator(symbol, source, target)
            for source, target in _AGGREGATION_CHAIN[start:]
        }
        if self.store is not None:
            self._restore_aggregators()

    def _restore_aggregators(self) -> None:
        """Repair forward higher bars and restore incomplete buckets.

        A recorder restart must not make the 1m→4h ladder wait for a completely
        new target bucket. Persisted closed source bars are authoritative, so
        rebuild only the forward tail after the latest stored target and prime
        each live aggregator with the remaining incomplete bucket. Historical
        gaps still fail closed through ``aggregate_candle_series``.
        """
        assert self.store is not None
        rebuilt_count = 0
        for source, target in _AGGREGATION_CHAIN:
            aggregator = self._aggregators.get(source)
            if aggregator is None:
                continue
            source_rows = self.store.read(self.symbol, source)
            if not source_rows:
                continue
            target_rows = self.store.read(self.symbol, target)
            existing_opens = {candle.open_time for candle in target_rows}
            complete = aggregate_candle_series(
                self.symbol,
                source,
                target,
                source_rows,
            )
            # A prior restart can leave an interior target hole even when a
            # newer target bar already exists. Reconstruct only bars whose
            # complete source bucket is present; source gaps therefore remain
            # visible and existing authoritative bars are never rewritten.
            rebuilt = [
                candle for candle in complete if candle.open_time not in existing_opens
            ]
            if rebuilt:
                self.store.upsert(rebuilt)
                rebuilt_count += len(rebuilt)
                target_rows = [*target_rows, *rebuilt]
            target_close = (
                max(candle.close_time for candle in target_rows)
                if target_rows
                else None
            )
            pending = [
                candle
                for candle in source_rows
                if target_close is None or candle.open_time >= target_close
            ]
            for candle in pending:
                aggregator.on_candle(candle)
        if rebuilt_count:
            logger.info(
                "restored canonical aggregation: symbol=%s rebuilt=%d",
                self.symbol,
                rebuilt_count,
            )

    def _publish(self, candle: Candle, published: list[Candle]) -> None:
        if not candle.is_closed:
            raise ValueError("pipeline may publish closed candles only")
        if self.store is not None:
            self.store.upsert((candle,))
        published.append(candle)
        for subscriber in self.subscribers:
            subscriber(candle)
        aggregator = self._aggregators.get(candle.timeframe)
        if aggregator is not None:
            higher = aggregator.on_candle(candle)
            if higher is not None:
                self._publish(higher, published)

    def on_trade(
        self,
        timestamp: datetime,
        price: Decimal | int | str,
        amount: Decimal | int | str,
        is_buyer_maker: bool | None = None,
    ) -> tuple[Candle, ...]:
        try:
            closed = self.builder.on_trade(timestamp, price, amount, is_buyer_maker)
        except ValueError as exc:
            rejected = RejectedTrade(
                symbol=self.symbol,
                timeframe=self.builder.timeframe,
                timestamp=timestamp,
                price=str(price),
                amount=str(amount),
                is_buyer_maker=is_buyer_maker,
                reason=str(exc),
            )
            logger.warning(
                "candle trade rejected: symbol=%s timeframe=%s timestamp=%s reason=%s",
                rejected.symbol,
                rejected.timeframe,
                rejected.timestamp,
                rejected.reason,
            )
            if self.rejected_trade_sink is not None:
                try:
                    self.rejected_trade_sink(rejected)
                except Exception:
                    # Sink failure must not mask or replace the original rejection.
                    logger.exception("rejected-trade sink failed")
            raise
        published: list[Candle] = []
        if closed is not None:
            self._publish(closed, published)
        return tuple(published)

    def advance_time(self, now: datetime) -> tuple[Candle, ...]:
        """Publish the forming bar iff its ``close_time <= now``.

        Only already-observed trade state is finalized. Elapsed empty buckets
        are not synthesized.
        """
        closed = self.builder.close_if_elapsed(now)
        published: list[Candle] = []
        if closed is not None:
            self._publish(closed, published)
        return tuple(published)

    def forming(self) -> Candle | None:
        return self.builder.forming()

    def rebuild_forming(
        self,
        bucket_open: datetime,
        trades: Sequence[Trade],
    ) -> Candle | None:
        """Replace only the unpersisted forming bucket from authoritative trades.

        Closed candles, higher-timeframe aggregators, subscribers, and Parquet
        are deliberately untouched. An empty authoritative bucket drops the
        forming candle instead of creating zero-volume OHLC.
        """
        bucket_open = floor_time(bucket_open, self.builder.timeframe)
        closed_through = self.builder.closed_through
        if closed_through is not None and bucket_open < closed_through:
            raise ValueError("cannot rebuild a bucket inside immutable closed history")
        replacement = CandleBuilder(self.symbol, self.builder.timeframe)
        replacement._closed_through = closed_through
        for trade in sorted(trades, key=lambda item: item.timestamp):
            if floor_time(trade.timestamp, replacement.timeframe) != bucket_open:
                raise ValueError("forming-bar rebuild contains a trade from another bucket")
            if (
                replacement.on_trade(
                    trade.timestamp,
                    trade.price,
                    trade.amount,
                    trade.is_buyer_maker,
                )
                is not None
            ):
                raise ValueError("forming-bar rebuild attempted to close a candle")
        self.builder = replacement
        return self.builder.forming()


def build_candles_from_trades(
    symbol: str,
    trades: Iterable[Trade],
    *,
    base_timeframe: str = "1m",
    close_through: datetime | None = None,
) -> dict[str, tuple[Candle, ...]]:
    """Deterministically replay trades into closed candles for research.

    Trades are stably sorted by timestamp.  The final forming minute remains
    unpublished unless ``close_through`` reaches its exclusive close boundary.
    """
    if base_timeframe not in {"1s", "1m"}:
        raise ValueError("research candle base_timeframe must be '1s' or '1m'")
    output: dict[str, list[Candle]] = {
        timeframe: [] for timeframe in TF_SECONDS if base_timeframe == "1s" or timeframe != "1s"
    }

    def capture(candle: Candle) -> None:
        output[candle.timeframe].append(candle)

    pipeline = CandlePipeline(symbol, base_timeframe=base_timeframe, subscribers=(capture,))
    for trade in sorted(trades, key=lambda item: item.timestamp):
        pipeline.on_trade(
            trade.timestamp,
            trade.price,
            trade.amount,
            trade.is_buyer_maker,
        )
    if close_through is not None:
        pipeline.advance_time(close_through)
    return {timeframe: tuple(candles) for timeframe, candles in output.items()}


def trades_from_tick_frame(frame: pd.DataFrame) -> tuple[Trade, ...]:
    """Convert the VNEDGE tick-lake trade schema into canonical trades.

    The recorder's ``side`` is the aggressor side: ``buy`` means the buyer is
    the taker (``is_buyer_maker=False``), while ``sell`` means the buyer made.
    Unknown/blank sides preserve ``None`` rather than inventing order flow.
    """
    required = {"ts_ms", "price", "amount"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"tick frame missing required columns: {sorted(missing)}")
    if frame.empty:
        return ()
    ordered = frame.sort_values("ts_ms", kind="stable")
    output = []
    for row in ordered.to_dict("records"):
        side = str(row.get("side") or "").strip().lower()
        buyer_maker = False if side == "buy" else True if side == "sell" else None
        output.append(
            Trade(
                timestamp=pd.Timestamp(row["ts_ms"], unit="ms", tz="UTC").to_pydatetime(),
                price=Decimal(str(row["price"])),
                amount=Decimal(str(row["amount"])),
                is_buyer_maker=buyer_maker,
            )
        )
    return tuple(output)
