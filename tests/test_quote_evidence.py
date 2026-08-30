from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from vnedge.exchange.book_imbalance import imbalance_l1
from vnedge.exchange.live_feed import QuoteUpdate
from vnedge.runtime.quote_evidence import QuoteEvidenceRecorder


@pytest.mark.asyncio
async def test_quote_evidence_persists_exact_consumed_quote(tmp_path: Path) -> None:
    recorder = QuoteEvidenceRecorder(
        tmp_path,
        lane_id="shadow/BTC 5m",
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        flush_every=2,
        flush_seconds=60,
    )
    recorder.start()
    timestamp = datetime(2026, 8, 29, 5, tzinfo=UTC)
    for sequence, price in ((101, 77_000.0), ("native-102", 77_001.0)):
        recorder.record(
            QuoteUpdate(
                ts=timestamp + timedelta(milliseconds=sequence if isinstance(sequence, int) else 2),
                received_ts=timestamp + timedelta(milliseconds=10),
                bid=price,
                ask=price + 0.1,
                sequence=sequence,
                source="test:shared_feed",
                exchange_timestamped=True,
            ),
            source_overflow_drops=3,
        )
    await recorder.close()

    shards = list(tmp_path.rglob("*.parquet"))
    assert len(shards) == 1
    frame = pd.read_parquet(shards[0])
    assert frame["sequence"].tolist() == ["101", "native-102"]
    assert frame["overflow_drops"].tolist() == [3, 3]
    assert frame["symbol"].unique().tolist() == ["BTCUSDT"]
    assert recorder.snapshot()["rows_persisted"] == 2
    assert recorder.snapshot()["healthy"] is True


@pytest.mark.asyncio
async def test_quote_evidence_persists_sized_book_filter_inputs(tmp_path: Path) -> None:
    recorder = QuoteEvidenceRecorder(
        tmp_path,
        lane_id="delta-btc",
        exchange="delta_india",
        symbol="BTCUSD",
        flush_every=1,
    )
    recorder.start()
    timestamp = datetime(2026, 8, 30, 12, tzinfo=UTC)
    book = imbalance_l1(68_518.0, 68_519.0, 2_452.0, 285.0, tick=0.5, ts=timestamp)
    assert book is not None
    recorder.record(
        QuoteUpdate(
            ts=timestamp,
            received_ts=timestamp + timedelta(milliseconds=50),
            bid=book.bid,
            ask=book.ask,
            sequence=100,
            source="delta_india:ob_l1",
            exchange_timestamped=True,
            book=book,
        ),
        source_overflow_drops=0,
    )
    await recorder.close()
    frame = pd.read_parquet(next(tmp_path.rglob("*.parquet")))
    assert frame.loc[0, "bid_size"] == 2_452.0
    assert frame.loc[0, "ask_size"] == 285.0
    assert frame.loc[0, "book_imbalance"] == pytest.approx(book.imb)
    assert frame.loc[0, "spread_ticks"] == 2.0


def test_quote_evidence_queue_overflow_invalidates_window(tmp_path: Path) -> None:
    recorder = QuoteEvidenceRecorder(
        tmp_path,
        lane_id="lane",
        exchange="binanceusdm",
        symbol="BTCUSDT",
        max_queue=1,
    )
    quote = QuoteUpdate(
        ts=datetime(2026, 8, 29, 5, tzinfo=UTC),
        bid=100.0,
        ask=100.1,
    )
    recorder.record(quote, source_overflow_drops=0)
    recorder.record(quote, source_overflow_drops=0)
    snapshot = recorder.snapshot()
    assert snapshot["rows_accepted"] == 1
    assert snapshot["queue_overflow_drops"] == 1
    assert snapshot["healthy"] is False


def test_quote_evidence_refuses_crossed_input_before_persistence(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="below bid"):
        QuoteUpdate(
            ts=datetime(2026, 8, 29, 5, tzinfo=UTC),
            bid=101.0,
            ask=100.0,
        )
