"""Exchange-stream integrity and explicit gap audit records.

Quiet markets are not gaps. A gap exists only when continuous coverage was
expected but connectivity, sequence continuity, backfill, or storage cannot be
proven. This module is measurement infrastructure: it exposes a hard
``entries_blocked`` state but never emits orders or grants trading permission.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from itertools import pairwise
from pathlib import Path

import pandas as pd

from vnedge.data.candles import (
    Candle,
    CandleParquetStore,
    CandlePipeline,
    JsonlTradeQuarantine,
    Trade,
    floor_time,
)
from vnedge.data.parquet_store import sanitize_symbol

logger = logging.getLogger(__name__)


def _utc(timestamp: datetime, *, label: str) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return timestamp.astimezone(UTC)


def _positive_decimal(value: Decimal | float | str, *, label: str) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return result


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class GapKind(str, Enum):
    STREAM_STALE = "stream_stale"
    SEQ_BREAK = "seq_break"
    OUT_OF_ORDER = "out_of_order"
    CLOCK_SKEW = "clock_skew"
    BACKFILL_FAIL = "backfill_fail"
    STORAGE_HOLE = "storage_hole"
    LATE_TRADE = "late_trade"


class DataQuality(str, Enum):
    """Operator-facing quality state; only OK/QUIET may arm new risk."""

    OK = "ok"
    QUIET = "quiet"
    DEGRADED = "degraded"
    GAP = "gap"
    STALE = "stale"


class RecoveryPhase(str, Enum):
    """Explicit path from a tripped integrity guard back to healthy."""

    NONE = "none"
    RESYNCING = "resyncing"
    BACKFILLING = "backfilling"
    PROBING = "probing"
    AWAITING_ACK = "awaiting_ack"


@dataclass(frozen=True, slots=True)
class RecoveryConfig:
    """Pre-registered proof required before a gap may be cleared."""

    probe_good_msgs: int = 50
    probe_good_closed_bars: int = 2
    max_backfill_retries: int = 3
    require_operator_ack_live: bool = False

    def __post_init__(self) -> None:
        if self.probe_good_msgs < 1:
            raise ValueError("probe_good_msgs must be positive")
        if self.probe_good_closed_bars < 1:
            raise ValueError("probe_good_closed_bars must be positive")
        if self.max_backfill_retries < 1:
            raise ValueError("max_backfill_retries must be positive")


@dataclass(frozen=True, slots=True)
class GapRecord:
    symbol: str
    exchange: str
    kind: GapKind
    start: datetime
    end: datetime
    detected_at: datetime
    detail: str = ""
    recovered: bool = False
    gap_id: str = ""

    def __post_init__(self) -> None:
        symbol = self.symbol.strip()
        exchange = self.exchange.strip()
        if not symbol or not exchange:
            raise ValueError("gap symbol and exchange must not be empty")
        kind = self.kind if isinstance(self.kind, GapKind) else GapKind(self.kind)
        start = _utc(self.start, label="gap start")
        end = _utc(self.end, label="gap end")
        detected_at = _utc(self.detected_at, label="gap detected_at")
        if end < start:
            raise ValueError("gap end must not precede start")
        gap_id = self.gap_id.strip()
        if not gap_id:
            identity = (
                f"{exchange}|{symbol}|{kind.value}|{start.isoformat()}|{detected_at.isoformat()}"
            )
            gap_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "detected_at", detected_at)
        object.__setattr__(self, "gap_id", gap_id)


@dataclass(frozen=True, slots=True)
class GapWriteResult:
    paths: tuple[Path, ...]
    records_written: int


class GapParquetStore:
    """Locked, idempotent Parquet audit store keyed by stable ``gap_id``."""

    _COLUMNS = (
        "gap_id",
        "symbol",
        "exchange",
        "kind",
        "start",
        "end",
        "detected_at",
        "detail",
        "recovered",
    )

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def partition_path(self, record: GapRecord) -> Path:
        exchange = re.sub(r"[^A-Za-z0-9_.-]+", "_", record.exchange)
        return (
            self.root
            / f"exchange={exchange}"
            / f"symbol={sanitize_symbol(record.symbol)}"
            / f"{record.detected_at:%Y-%m-%d}.parquet"
        )

    @classmethod
    def _frame(cls, records: Sequence[GapRecord]) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "gap_id": record.gap_id,
                    "symbol": record.symbol,
                    "exchange": record.exchange,
                    "kind": record.kind.value,
                    "start": record.start,
                    "end": record.end,
                    "detected_at": record.detected_at,
                    "detail": record.detail,
                    "recovered": record.recovered,
                }
                for record in records
            ],
            columns=cls._COLUMNS,
        )

    @staticmethod
    def _write_atomic(path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        frame.to_parquet(tmp, index=False)
        os.replace(tmp, path)

    def upsert(self, records: Iterable[GapRecord]) -> GapWriteResult:
        groups: dict[Path, list[GapRecord]] = {}
        for record in records:
            groups.setdefault(self.partition_path(record), []).append(record)
        for path, partition in groups.items():
            new_frame = self._frame(partition)
            with _exclusive_lock(path.with_suffix(f"{path.suffix}.lock")):
                if path.exists():
                    frame = pd.concat([pd.read_parquet(path), new_frame], ignore_index=True)
                else:
                    frame = new_frame
                frame = frame.drop_duplicates(subset="gap_id", keep="last")
                frame = frame.sort_values(["detected_at", "gap_id"]).reset_index(drop=True)
                self._write_atomic(path, frame)
        return GapWriteResult(tuple(sorted(groups)), sum(len(rows) for rows in groups.values()))

    def read(self, exchange: str, symbol: str) -> list[GapRecord]:
        safe_exchange = re.sub(r"[^A-Za-z0-9_.-]+", "_", exchange.strip())
        directory = self.root / f"exchange={safe_exchange}" / f"symbol={sanitize_symbol(symbol)}"
        frames = [pd.read_parquet(path) for path in sorted(directory.glob("*.parquet"))]
        if not frames:
            return []
        frame = pd.concat(frames, ignore_index=True)
        frame = frame.drop_duplicates(subset="gap_id", keep="last")
        frame = frame.sort_values(["detected_at", "gap_id"])
        return [
            GapRecord(
                symbol=row["symbol"],
                exchange=row["exchange"],
                kind=GapKind(row["kind"]),
                start=row["start"].to_pydatetime(),
                end=row["end"].to_pydatetime(),
                detected_at=row["detected_at"].to_pydatetime(),
                detail=row["detail"],
                recovered=bool(row["recovered"]),
                gap_id=row["gap_id"],
            )
            for row in frame.to_dict("records")
        ]


@dataclass(frozen=True, slots=True)
class IdentifiedTrade:
    trade_id: str
    timestamp: datetime
    price: Decimal
    amount: Decimal
    is_buyer_maker: bool | None = None

    def __post_init__(self) -> None:
        trade_id = self.trade_id.strip()
        if not trade_id:
            raise ValueError("exchange trade_id must not be empty")
        object.__setattr__(self, "trade_id", trade_id)
        object.__setattr__(self, "timestamp", _utc(self.timestamp, label="trade timestamp"))
        object.__setattr__(self, "price", _positive_decimal(self.price, label="trade price"))
        object.__setattr__(self, "amount", _positive_decimal(self.amount, label="trade amount"))

    def candle_trade(self) -> Trade:
        return Trade(self.timestamp, self.price, self.amount, self.is_buyer_maker)


def merge_identified_trades(
    existing: Iterable[IdentifiedTrade],
    incoming: Iterable[IdentifiedTrade],
) -> tuple[IdentifiedTrade, ...]:
    """Idempotently merge overlap backfill by exchange trade ID."""
    by_id: dict[str, IdentifiedTrade] = {}
    for trade in (*tuple(existing), *tuple(incoming)):
        previous = by_id.get(trade.trade_id)
        if previous is not None and previous != trade:
            raise ValueError(f"conflicting payload for trade_id {trade.trade_id!r}")
        by_id[trade.trade_id] = trade
    return tuple(sorted(by_id.values(), key=lambda trade: (trade.timestamp, trade.trade_id)))


class StreamIntegrityGuard:
    """Heartbeat/sequence state whose degraded state blocks new entries."""

    def __init__(
        self,
        exchange: str,
        symbol: str,
        *,
        stale_after: timedelta,
        monitoring_started_at: datetime,
        store: GapParquetStore | None = None,
        max_event_age: timedelta | None = None,
        max_clock_skew: timedelta = timedelta(seconds=5),
        recovery: RecoveryConfig | None = None,
    ) -> None:
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        if max_event_age is not None and max_event_age <= timedelta(0):
            raise ValueError("max_event_age must be positive")
        if max_clock_skew < timedelta(0):
            raise ValueError("max_clock_skew must be non-negative")
        self.exchange = exchange.strip()
        self.symbol = symbol.strip()
        if not self.exchange or not self.symbol:
            raise ValueError("guard exchange and symbol must not be empty")
        self.stale_after = stale_after
        self.max_event_age = max_event_age or stale_after
        self.max_clock_skew = max_clock_skew
        self.recovery = recovery or RecoveryConfig()
        self.store = store
        started = _utc(monitoring_started_at, label="monitoring_started_at")
        self._last_message_at: datetime = started
        self._last_event_time: datetime | None = None
        self._last_sequence: int | None = None
        self._active: dict[str, GapRecord] = {}
        self._data_degraded = False
        self._warm = False
        self._quiet = False
        self._recovery_phase = RecoveryPhase.NONE
        self._recovery_reason = ""
        self._healthy_recovery_messages = 0
        self._healthy_recovery_bars = 0
        self._backfill_attempts = 0
        self._operator_acked = False
        self._recovery_detail = ""

    def _persist(self, record: GapRecord) -> None:
        if self.store is not None:
            self.store.upsert((record,))

    def _open_gap(
        self,
        kind: GapKind,
        start: datetime,
        end: datetime,
        detected_at: datetime,
        detail: str,
    ) -> GapRecord:
        for record in self._active.values():
            if record.kind == kind:
                # A repeated failure of the same kind still invalidates an
                # in-progress probe. Recovery evidence must be consecutive.
                self._data_degraded = True
                self._warm = False
                self._quiet = False
                self._recovery_phase = RecoveryPhase.NONE
                self._recovery_reason = detail
                self._healthy_recovery_messages = 0
                self._healthy_recovery_bars = 0
                self._operator_acked = False
                self._recovery_detail = ""
                return record
        was_healthy = not self._data_degraded
        record = GapRecord(
            self.symbol,
            self.exchange,
            kind,
            start,
            end,
            detected_at,
            detail,
        )
        self._active[record.gap_id] = record
        self._data_degraded = True
        self._warm = False
        self._quiet = False
        self._recovery_phase = RecoveryPhase.NONE
        self._recovery_reason = detail
        self._healthy_recovery_messages = 0
        self._healthy_recovery_bars = 0
        self._operator_acked = False
        self._recovery_detail = ""
        if was_healthy:
            self._backfill_attempts = 0
        self._persist(record)
        logger.error(
            "exchange data degraded: exchange=%s symbol=%s kind=%s detail=%s",
            self.exchange,
            self.symbol,
            kind.value,
            detail,
        )
        return record

    def on_message(
        self,
        received_at: datetime,
        *,
        event_time: datetime | None = None,
        sequence_id: int | None = None,
    ) -> bool:
        received_at = _utc(received_at, label="message received_at")
        if received_at < self._last_message_at:
            raise ValueError("message receive times must be ordered")
        event_time = (
            _utc(event_time, label="message event_time") if event_time is not None else None
        )
        message_valid = True
        if received_at - self._last_message_at > self.stale_after:
            message_valid = False
            self._open_gap(
                GapKind.STREAM_STALE,
                self._last_message_at,
                received_at,
                received_at,
                f"message interval exceeded {self.stale_after.total_seconds():g}s",
            )
        if event_time is not None:
            event_age = received_at - event_time
            if event_age > self.max_event_age:
                message_valid = False
                self._open_gap(
                    GapKind.LATE_TRADE,
                    event_time,
                    received_at,
                    received_at,
                    f"event age {event_age.total_seconds():.3f}s exceeds "
                    f"{self.max_event_age.total_seconds():g}s",
                )
            elif event_age < -self.max_clock_skew:
                message_valid = False
                self._open_gap(
                    GapKind.CLOCK_SKEW,
                    received_at,
                    event_time,
                    received_at,
                    f"event is {-event_age.total_seconds():.3f}s in the future",
                )
            if self._last_event_time is not None and event_time < self._last_event_time:
                message_valid = False
                self._open_gap(
                    GapKind.OUT_OF_ORDER,
                    event_time,
                    self._last_event_time,
                    received_at,
                    "event timestamp moved backward",
                )
        if (
            sequence_id is not None
            and self._last_sequence is not None
            and sequence_id != self._last_sequence + 1
        ):
            message_valid = False
            start = self._last_event_time or self._last_message_at
            self._open_gap(
                GapKind.SEQ_BREAK,
                start,
                event_time or received_at,
                received_at,
                f"expected sequence {self._last_sequence + 1}, received {sequence_id}",
            )
        self._last_message_at = received_at
        if event_time is not None and (
            self._last_event_time is None or event_time > self._last_event_time
        ):
            self._last_event_time = event_time
        if sequence_id is not None:
            self._last_sequence = sequence_id
        if self._recovery_phase in {
            RecoveryPhase.PROBING,
            RecoveryPhase.AWAITING_ACK,
        }:
            if message_valid:
                self._quiet = event_time is None
                self._healthy_recovery_messages += 1
                self._try_complete_recovery(received_at)
        elif not self._data_degraded:
            self._quiet = event_time is None
            self._warm = True
        return not self.entries_blocked

    def check_stale(self, now: datetime) -> bool:
        now = _utc(now, label="stale check time")
        if now - self._last_message_at > self.stale_after:
            self._open_gap(
                GapKind.STREAM_STALE,
                self._last_message_at,
                now,
                now,
                f"last message age exceeded {self.stale_after.total_seconds():g}s",
            )
        return self._data_degraded

    def record_late_trade(self, timestamp: datetime, detected_at: datetime, detail: str) -> None:
        timestamp = _utc(timestamp, label="late trade timestamp")
        detected_at = _utc(detected_at, label="late trade detected_at")
        self._open_gap(
            GapKind.LATE_TRADE,
            timestamp,
            timestamp,
            detected_at,
            detail,
        )

    def backfill_window(
        self,
        now: datetime,
        *,
        overlap: timedelta = timedelta(minutes=2),
    ) -> tuple[datetime, datetime]:
        if overlap < timedelta(0):
            raise ValueError("backfill overlap must be non-negative")
        now = _utc(now, label="backfill end")
        start = (self._last_event_time or self._last_message_at) - overlap
        return start, now

    def recover(self, at: datetime, *, continuity_proven: bool, detail: str = "") -> bool:
        at = _utc(at, label="recovery time")
        if not continuity_proven:
            self._backfill_attempts += 1
            start = min(
                (record.start for record in self._active.values()),
                default=self._last_event_time or self._last_message_at,
            )
            self._open_gap(
                GapKind.BACKFILL_FAIL,
                start,
                at,
                at,
                detail or "backfill did not prove continuity",
            )
            self._recovery_phase = RecoveryPhase.BACKFILLING
            self._recovery_reason = (
                "backfill_exhausted"
                if self._backfill_attempts >= self.recovery.max_backfill_retries
                else "backfill_failed"
            )
            return False
        if not self._active and not self._data_degraded:
            return True
        self._recovery_phase = RecoveryPhase.PROBING
        self._recovery_reason = "probing"
        self._recovery_detail = detail
        self._data_degraded = True
        self._warm = False
        self._quiet = False
        self._healthy_recovery_messages = 0
        self._healthy_recovery_bars = 0
        self._last_message_at = at
        self._last_sequence = None
        return True

    def begin_recovery(self, phase: RecoveryPhase) -> None:
        """Expose resync/backfill work as an entry-blocking state."""
        if phase not in {RecoveryPhase.RESYNCING, RecoveryPhase.BACKFILLING}:
            raise ValueError("recovery may only begin in resyncing or backfilling")
        if not self._active:
            raise RuntimeError("cannot begin recovery without an active gap")
        self._recovery_phase = phase
        self._recovery_reason = phase.value
        self._warm = False

    def on_good_closed_bar(self, at: datetime) -> bool:
        """Count a causally closed probe bar without altering stored candles."""
        at = _utc(at, label="probe bar time")
        if self._recovery_phase not in {
            RecoveryPhase.PROBING,
            RecoveryPhase.AWAITING_ACK,
        }:
            return not self.entries_blocked
        self._healthy_recovery_bars += 1
        self._try_complete_recovery(at)
        return not self.entries_blocked

    def operator_ack(self, at: datetime, *, note: str) -> bool:
        """Acknowledge a proven recovery; acknowledgement never proves continuity."""
        at = _utc(at, label="operator acknowledgement time")
        note = note.strip()
        if not note:
            raise ValueError("operator acknowledgement requires a note")
        if not self._active:
            return False
        self._operator_acked = True
        self._recovery_detail = "; ".join(
            part for part in (self._recovery_detail, f"operator_ack={note}") if part
        )
        self._try_complete_recovery(at)
        return not self.entries_blocked

    def _probe_passed(self) -> bool:
        return (
            self._healthy_recovery_messages >= self.recovery.probe_good_msgs
            or self._healthy_recovery_bars >= self.recovery.probe_good_closed_bars
        )

    def _try_complete_recovery(self, at: datetime) -> None:
        if not self._probe_passed():
            self._recovery_reason = (
                f"probing messages={self._healthy_recovery_messages}/"
                f"{self.recovery.probe_good_msgs} bars={self._healthy_recovery_bars}/"
                f"{self.recovery.probe_good_closed_bars}"
            )
            return
        if self.recovery.require_operator_ack_live and not self._operator_acked:
            self._recovery_phase = RecoveryPhase.AWAITING_ACK
            self._recovery_reason = "await_operator_ack"
            return
        for gap_id, record in tuple(self._active.items()):
            recovered = replace(
                record,
                detail="; ".join(
                    part for part in (record.detail, self._recovery_detail) if part
                ),
                recovered=True,
            )
            self._persist(recovered)
            self._active[gap_id] = recovered
        self._active.clear()
        self._data_degraded = False
        self._warm = True
        self._recovery_phase = RecoveryPhase.NONE
        self._recovery_reason = "recovered"
        self._healthy_recovery_messages = 0
        self._healthy_recovery_bars = 0
        self._backfill_attempts = 0

    @property
    def data_degraded(self) -> bool:
        return self._data_degraded

    @property
    def stream_stale(self) -> bool:
        return any(record.kind == GapKind.STREAM_STALE for record in self._active.values())

    @property
    def entries_blocked(self) -> bool:
        return (
            self._data_degraded
            or not self._warm
            or self._recovery_phase != RecoveryPhase.NONE
        )

    @property
    def quality(self) -> DataQuality:
        if self._recovery_phase in {
            RecoveryPhase.RESYNCING,
            RecoveryPhase.PROBING,
            RecoveryPhase.AWAITING_ACK,
        }:
            return DataQuality.DEGRADED
        if self._data_degraded:
            if any(record.kind == GapKind.STREAM_STALE for record in self._active.values()):
                return DataQuality.STALE
            return DataQuality.GAP
        if not self._warm:
            return DataQuality.DEGRADED
        return DataQuality.QUIET if self._quiet else DataQuality.OK

    @property
    def recovery_phase(self) -> RecoveryPhase:
        return self._recovery_phase

    @property
    def recovery_reason(self) -> str:
        return self._recovery_reason

    @property
    def backfill_attempts(self) -> int:
        return self._backfill_attempts

    @property
    def exits_allowed(self) -> bool:
        return True

    @property
    def active_gaps(self) -> tuple[GapRecord, ...]:
        return tuple(self._active.values())


class GapAwareCandlePipeline:
    """Candle pipeline frozen during unknown coverage and healed by trade-ID backfill."""

    def __init__(
        self,
        exchange: str,
        symbol: str,
        *,
        monitoring_started_at: datetime,
        stale_after: timedelta = timedelta(seconds=10),
        candle_store: CandleParquetStore | None = None,
        gap_store: GapParquetStore | None = None,
        quarantine: JsonlTradeQuarantine | None = None,
        recovery: RecoveryConfig | None = None,
    ) -> None:
        self.pipeline = CandlePipeline(
            symbol,
            store=candle_store,
            rejected_trade_sink=quarantine,
        )
        self.guard = StreamIntegrityGuard(
            exchange,
            symbol,
            stale_after=stale_after,
            monitoring_started_at=monitoring_started_at,
            store=gap_store,
            recovery=recovery,
        )
        self._forming_bucket: datetime | None = None
        self._forming_trades: dict[str, IdentifiedTrade] = {}

    def on_heartbeat(self, received_at: datetime, *, sequence_id: int | None = None) -> bool:
        return self.guard.on_message(received_at, sequence_id=sequence_id)

    def on_trade(
        self, trade: IdentifiedTrade, *, received_at: datetime, sequence_id: int | None = None
    ) -> tuple[Candle, ...]:
        healthy = self.guard.on_message(
            received_at,
            event_time=trade.timestamp,
            sequence_id=sequence_id,
        )
        if not healthy:
            logger.warning("trade withheld while stream continuity is degraded: %s", trade.trade_id)
            return ()
        bucket = floor_time(trade.timestamp, self.pipeline.builder.timeframe)
        previous = self._forming_trades.get(trade.trade_id)
        if previous is not None:
            if previous != trade:
                detail = f"conflicting live payload for trade_id {trade.trade_id!r}"
                self.guard.record_late_trade(trade.timestamp, received_at, detail)
                raise ValueError(detail)
            return ()
        try:
            published = self.pipeline.on_trade(
                trade.timestamp,
                trade.price,
                trade.amount,
                trade.is_buyer_maker,
            )
        except ValueError as exc:
            self.guard.record_late_trade(trade.timestamp, received_at, str(exc))
            raise
        if bucket != self._forming_bucket:
            self._forming_bucket = bucket
            self._forming_trades.clear()
        self._forming_trades[trade.trade_id] = trade
        return published

    def advance_time(self, now: datetime) -> tuple[Candle, ...]:
        if self.guard.check_stale(now):
            return ()
        return self.pipeline.advance_time(now)

    def recover(
        self,
        backfill: Iterable[IdentifiedTrade],
        *,
        at: datetime,
        continuity_proven: bool,
        detail: str = "",
    ) -> bool:
        if self.guard.active_gaps:
            resync_kinds = {GapKind.SEQ_BREAK, GapKind.STREAM_STALE}
            phase = (
                RecoveryPhase.RESYNCING
                if any(record.kind in resync_kinds for record in self.guard.active_gaps)
                else RecoveryPhase.BACKFILLING
            )
            self.guard.begin_recovery(phase)
        if not continuity_proven:
            return self.guard.recover(at, continuity_proven=False, detail=detail)
        try:
            merged = merge_identified_trades(self._forming_trades.values(), backfill)
        except ValueError as exc:
            self.guard.recover(at, continuity_proven=False, detail=str(exc))
            raise
        bucket = floor_time(at, self.pipeline.builder.timeframe)
        current = tuple(
            trade
            for trade in merged
            if floor_time(trade.timestamp, self.pipeline.builder.timeframe) == bucket
        )
        self.pipeline.rebuild_forming(bucket, tuple(trade.candle_trade() for trade in current))
        self._forming_bucket = bucket if current else None
        self._forming_trades = {trade.trade_id: trade for trade in current}
        return self.guard.recover(at, continuity_proven=True, detail=detail)

    def on_recovery_closed_bar(self, at: datetime) -> bool:
        return self.guard.on_good_closed_bar(at)

    def acknowledge_recovery(self, at: datetime, *, note: str) -> bool:
        return self.guard.operator_ack(at, note=note)

    @property
    def entries_blocked(self) -> bool:
        return self.guard.entries_blocked

    @property
    def data_degraded(self) -> bool:
        return self.guard.data_degraded

    @property
    def data_quality(self) -> DataQuality:
        return self.guard.quality

    @property
    def exits_allowed(self) -> bool:
        return self.guard.exits_allowed


def candles_without_gaps(
    candles: Iterable[Candle],
    gaps: Iterable[GapRecord],
) -> tuple[Candle, ...]:
    """Drop research candles whose interval overlaps a recorded integrity gap."""
    gap_list = tuple(gaps)
    return tuple(
        candle
        for candle in candles
        if not any(
            gap.symbol == candle.symbol
            and not gap.recovered
            and gap.start < candle.close_time
            and gap.end > candle.open_time
            for gap in gap_list
        )
    )


def coverage_fraction(start: datetime, end: datetime, gaps: Iterable[GapRecord]) -> Decimal:
    """Return proven coverage in ``[0, 1]`` after unioning overlapping gaps."""
    start = _utc(start, label="coverage start")
    end = _utc(end, label="coverage end")
    if end <= start:
        raise ValueError("coverage end must be after start")
    intervals = sorted(
        (max(start, gap.start), min(end, gap.end))
        for gap in gaps
        if not gap.recovered and gap.end > start and gap.start < end
    )
    merged: list[tuple[datetime, datetime]] = []
    for interval_start, interval_end in intervals:
        if interval_end <= interval_start:
            continue
        if not merged or interval_start > merged[-1][1]:
            merged.append((interval_start, interval_end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], interval_end))
    missing = sum(
        (
            Decimal(str((interval_end - interval_start).total_seconds()))
            for interval_start, interval_end in merged
        ),
        Decimal(0),
    )
    total = Decimal(str((end - start).total_seconds()))
    return (total - missing) / total


def storage_holes_from_days(
    exchange: str,
    symbol: str,
    expected_days: Iterable[date],
    present_days: Iterable[date],
    *,
    detected_at: datetime,
) -> tuple[GapRecord, ...]:
    """Turn an expected-vs-present shard inventory into auditable day gaps."""
    detected_at = _utc(detected_at, label="storage-hole detected_at")
    missing = sorted(set(expected_days) - set(present_days))
    records = []
    for day in missing:
        start = datetime.combine(day, time.min, tzinfo=UTC)
        records.append(
            GapRecord(
                symbol,
                exchange,
                GapKind.STORAGE_HOLE,
                start,
                start + timedelta(days=1),
                detected_at,
                "expected shard day is absent",
            )
        )
    return tuple(records)


def offline_trade_time_holes(
    exchange: str,
    symbol: str,
    trades: Iterable[IdentifiedTrade],
    *,
    max_expected_gap: timedelta,
    detected_at: datetime,
    continuous_coverage_expected: bool,
    expected_start: datetime | None = None,
    expected_end: datetime | None = None,
) -> tuple[GapRecord, ...]:
    """Flag suspicious offline intervals only when coverage was expected.

    Trade-time distance alone cannot distinguish a quiet market from lost
    data. Callers must explicitly assert that a subscription or complete
    historical page range covered the interval. Optional bounds also detect a
    leading or trailing hole around the sorted ``(timestamp, trade_id)`` data.
    """
    if max_expected_gap <= timedelta(0):
        raise ValueError("max_expected_gap must be positive")
    detected_at = _utc(detected_at, label="offline-gap detected_at")
    if not continuous_coverage_expected:
        return ()

    ordered = sorted(trades, key=lambda trade: (trade.timestamp, trade.trade_id))
    points = [trade.timestamp for trade in ordered]
    if expected_start is not None:
        expected_start = _utc(expected_start, label="expected_start")
        points.insert(0, expected_start)
    if expected_end is not None:
        expected_end = _utc(expected_end, label="expected_end")
        points.append(expected_end)
    if expected_start is not None and expected_end is not None and expected_end <= expected_start:
        raise ValueError("expected_end must be after expected_start")

    holes: list[GapRecord] = []
    for previous, current in pairwise(points):
        if current < previous:
            raise ValueError("expected coverage bounds must contain the trade data")
        if current - previous <= max_expected_gap:
            continue
        holes.append(
            GapRecord(
                symbol,
                exchange,
                GapKind.STORAGE_HOLE,
                previous,
                current,
                detected_at,
                (
                    "offline trade interval exceeded expected maximum "
                    f"of {max_expected_gap.total_seconds():g}s"
                ),
            )
        )
    return tuple(holes)
