from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.runtime.context_refresh import exact_context_row, official_context_row

OPEN = datetime(2026, 9, 9, 20, tzinfo=UTC)
SOURCE = "exchange_ohlcv_validated"


def official():
    return official_context_row(
        [[int(OPEN.timestamp() * 1000), 100, 105, 95, 101, 50]],
        symbol="BTC/USD:USD",
        timeframe="4h",
        opened=OPEN,
        now=OPEN + timedelta(hours=4),
    )


def test_exact_official_context_retains_source_not_vwap_proof():
    row = official()
    assert row["candle_source"] == SOURCE
    assert len(row["content_sha256"]) == 64
    assert "quote_volume" not in row
    assert "trade_count" not in row
    assert (
        exact_context_row(
            pd.DataFrame([row]),
            symbol="BTCUSD",
            timeframe="4h",
            opened=OPEN,
            allowed_sources=("canonical_tick_lake",),
        )
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_closed", False),
        ("coverage_ok", False),
        ("data_quality", "partial"),
        ("symbol", "ETHUSD"),
        ("timeframe", "1d"),
        ("close", 102),
        ("content_sha256", "0" * 64),
        ("candle_source", "canonical_tick_lake"),
        ("close", float("nan")),
    ],
)
def test_context_rejects_invalid_or_mutated_identity(field, value):
    row = official()
    row[field] = value
    assert (
        exact_context_row(
            pd.DataFrame([row]),
            symbol="BTCUSD",
            timeframe="4h",
            opened=OPEN,
            allowed_sources=(SOURCE,),
        )
        is None
    )


def test_no_asof_carry_duplicate_or_forming():
    row = official()
    for frame, opened in [
        (pd.DataFrame([row]), OPEN + timedelta(hours=4)),
        (pd.DataFrame([row, row]), OPEN),
    ]:
        assert (
            exact_context_row(
                frame, symbol="BTCUSD", timeframe="4h", opened=opened, allowed_sources=(SOURCE,)
            )
            is None
        )
    assert (
        official_context_row(
            [[int(OPEN.timestamp() * 1000), 100, 105, 95, 101, 50]],
            symbol="BTCUSD",
            timeframe="4h",
            opened=OPEN,
            now=OPEN + timedelta(hours=3),
        )
        is None
    )


@pytest.mark.parametrize(
    "values", [[100, 99, 95, 101, 50], [100, 105, 95, 101, -1], [100, 105, 95, float("nan"), 50]]
)
def test_official_ohlcv_geometry_and_finite_validation(values):
    assert (
        official_context_row(
            [[int(OPEN.timestamp() * 1000), *values]],
            symbol="BTCUSD",
            timeframe="4h",
            opened=OPEN,
            now=OPEN + timedelta(hours=4),
        )
        is None
    )
