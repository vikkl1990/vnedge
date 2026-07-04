"""Tick/L2 recorder — L2 schema, NaN padding, atomic sharded writes."""

import math

import pandas as pd
import pytest

from vnedge.exchange.tick_recorder import TickRecorder, _book_row, _Buffer

DAY_TS = 1_751_000_000_000  # fixed ms timestamp


def _ob(n_bid, n_ask):
    return {
        "bids": [[100.0 - i * 0.1, 1.0 + i] for i in range(n_bid)],
        "asks": [[101.0 + i * 0.1, 2.0 + i] for i in range(n_ask)],
    }


def test_book_row_captures_full_ladder_with_l1_aliases():
    row = _book_row(_ob(12, 12), levels=10, ts_ms=DAY_TS)
    # level-0 L1 aliases equal ladder level 0 (backward compat)
    assert row["bid"] == row["bid_px_0"] == 100.0
    assert row["ask"] == row["ask_px_0"] == 101.0
    assert row["bid_qty"] == row["bid_qty_0"] == 1.0
    # captured 10 levels; deepest is index 9; sliced there (no level 10)
    assert row["bid_px_9"] == pytest.approx(100.0 - 9 * 0.1)
    assert row["ask_px_9"] == pytest.approx(101.0 + 9 * 0.1)
    assert "bid_px_10" not in row


def test_book_row_pads_missing_levels_with_nan():
    row = _book_row(_ob(3, 3), levels=10, ts_ms=DAY_TS)
    assert row["bid_px_2"] == pytest.approx(100.0 - 2 * 0.1)
    assert math.isnan(row["bid_px_3"])   # only 3 levels available
    assert row["bid_qty_3"] == 0.0


def test_levels_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        TickRecorder("binanceusdm", ["BTC/USDT:USDT"], tmp_path, levels=0)


def test_book_limit_is_venue_safe():
    # 50 is the smallest depth Bybit swaps accept AND Binance USDT-M accepts;
    # limit=5 (the old value) errored on Bybit.
    rec = TickRecorder("binanceusdm", ["BTC/USDT:USDT"], "/tmp", levels=10)
    assert rec._book_limit == 50


def _book_dir(tmp_path, day):
    return (tmp_path / "ticks" / "exchange=binanceusdm"
            / "symbol=BTCUSDT" / "stream=book" / day)


def test_buffer_writes_atomic_shards_never_rewrites(tmp_path):
    buf = _Buffer(tmp_path, "binanceusdm", "BTC/USDT:USDT", "book")
    buf.add({"ts_ms": DAY_TS, "bid": 100.0})
    buf.flush(0.0)
    buf.add({"ts_ms": DAY_TS + 1000, "bid": 100.1})
    buf.flush(1.0)

    day = pd.to_datetime(DAY_TS, unit="ms", utc=True).strftime("%Y%m%d")
    shard_dir = _book_dir(tmp_path, day)
    shards = sorted(shard_dir.glob("*.parquet"))
    assert len(shards) == 2                         # two flushes -> two shards
    assert not list(shard_dir.glob(".*.tmp"))       # no leftover temp files
    # legacy single rewritten file is never produced
    assert not (shard_dir.parent / f"{day}.parquet").exists()
    df = pd.concat([pd.read_parquet(s) for s in shards], ignore_index=True)
    assert list(df["bid"]) == [100.0, 100.1]        # data intact, ordered


def test_empty_flush_is_a_noop(tmp_path):
    buf = _Buffer(tmp_path, "binanceusdm", "BTC/USDT:USDT", "book")
    assert buf.flush(0.0) == 0
