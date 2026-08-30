"""Exact, bounded quote-input capture for live/replay parity evidence.

The standalone book recorder owns a different websocket connection from the
lane feed.  Its BBO stream is useful market evidence, but it cannot prove
quote-scanner parity because event timing and sequence are not identical.
This recorder taps quotes only after a lane dequeues them and writes immutable
Parquet shards asynchronously.  It never feeds decisions and queue overflow
invalidates, rather than silently weakens, a parity window.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from vnedge.data.market_records import LaneBBO
from vnedge.data.symbols import canonical_symbol
from vnedge.exchange.live_feed import QuoteUpdate


@dataclass(frozen=True, slots=True)
class QuoteEvidenceSnapshot:
    lane_id: str
    rows_accepted: int
    rows_persisted: int
    queue_depth: int
    queue_overflow_drops: int
    persist_errors: int
    healthy: bool
    root: str


class QuoteEvidenceRecorder:
    """Persist the exact ordered quotes consumed by one scanner lane."""

    def __init__(
        self,
        root: Path,
        *,
        lane_id: str,
        exchange: str,
        symbol: str,
        max_queue: int = 8192,
        flush_every: int = 500,
        flush_seconds: float = 30.0,
    ) -> None:
        if max_queue < 1 or flush_every < 1 or flush_seconds <= 0:
            raise ValueError("quote evidence queue and flush bounds must be positive")
        self.root = Path(root)
        self.lane_id = lane_id
        self.exchange = exchange.strip().lower()
        self.symbol = canonical_symbol(symbol)
        self.flush_every = flush_every
        self.flush_seconds = flush_seconds
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._file_sequence = 0
        self.rows_accepted = 0
        self.rows_persisted = 0
        self.queue_overflow_drops = 0
        self.persist_errors = 0

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name=f"quote-evidence-{self.lane_id}"
        )

    def record(self, quote: QuoteUpdate, *, source_overflow_drops: int) -> None:
        """Enqueue one quote without blocking the decision loop."""
        received = quote.received_ts or quote.ts
        row = LaneBBO(
            lane_id=self.lane_id,
            exchange=self.exchange,
            symbol=self.symbol,
            bid=quote.bid,
            ask=quote.ask,
            ts=quote.ts,
            received_ts=received,
            sequence=quote.sequence,
            source=quote.source,
            overflow_drops=int(source_overflow_drops),
            capture_overflow_drops=self.queue_overflow_drops,
            captured_at_ms=int(datetime.now(UTC).timestamp() * 1000),
            exchange_timestamped=quote.exchange_timestamped,
        ).storage_row()
        if quote.book is not None:
            row.update(
                {
                    "book_ts_ms": int(quote.book.ts.timestamp() * 1000),
                    "bid_size": quote.book.bid_size,
                    "ask_size": quote.book.ask_size,
                    "book_imbalance": quote.book.imb,
                    "microprice": quote.book.microprice,
                    "spread_ticks": quote.book.spread_ticks,
                    "book_levels": quote.book.levels,
                }
            )
        try:
            self._queue.put_nowait(row)
            self.rows_accepted += 1
        except asyncio.QueueFull:
            self.queue_overflow_drops += 1

    async def close(self) -> None:
        task = self._task
        if task is None:
            return
        self._closing = True
        await task
        self._task = None

    def snapshot(self) -> dict[str, Any]:
        return asdict(
            QuoteEvidenceSnapshot(
                lane_id=self.lane_id,
                rows_accepted=self.rows_accepted,
                rows_persisted=self.rows_persisted,
                queue_depth=self._queue.qsize(),
                queue_overflow_drops=self.queue_overflow_drops,
                persist_errors=self.persist_errors,
                healthy=self.queue_overflow_drops == 0 and self.persist_errors == 0,
                root=str(self.root),
            )
        )

    async def _run(self) -> None:
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        while not self._closing or not self._queue.empty():
            timeout = max(0.05, self.flush_seconds - (time.monotonic() - last_flush))
            try:
                batch.append(await asyncio.wait_for(self._queue.get(), timeout=timeout))
            except TimeoutError:
                pass
            due = (
                len(batch) >= self.flush_every
                or (batch and time.monotonic() - last_flush >= self.flush_seconds)
                or (self._closing and self._queue.empty())
            )
            if not due:
                continue
            pending, batch = batch, []
            try:
                written = await asyncio.to_thread(self._persist, pending)
            except (OSError, ValueError):
                # Evidence failure does not alter decisions. The unhealthy
                # snapshot makes the affected window unusable for parity.
                self.persist_errors += 1
            else:
                self.rows_persisted += written
            last_flush = time.monotonic()

    def _persist(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        frame = pd.DataFrame(rows)
        frame["_day"] = pd.to_datetime(
            frame["ts_ms"], unit="ms", utc=True
        ).dt.strftime("%Y%m%d")
        safe_lane = "".join(
            char if char.isalnum() or char in {"-", "_"} else "_"
            for char in self.lane_id
        )
        for day, chunk in frame.groupby("_day", sort=True):
            chunk = chunk.drop(columns="_day")
            directory = (
                self.root
                / f"lane={safe_lane}"
                / f"exchange={self.exchange}"
                / f"symbol={self.symbol}"
                / str(day)
            )
            directory.mkdir(parents=True, exist_ok=True)
            first_ts = int(chunk["ts_ms"].iloc[0])
            name = f"{first_ts}-{self._file_sequence:08d}.parquet"
            self._file_sequence += 1
            final = directory / name
            temporary = directory / f".{name}.tmp"
            chunk.to_parquet(temporary, index=False)
            os.replace(temporary, final)
        return len(frame)


__all__ = ["QuoteEvidenceRecorder", "QuoteEvidenceSnapshot"]
