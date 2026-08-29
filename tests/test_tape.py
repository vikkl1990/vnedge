"""Zero-price prints must never reach an analysis silently."""

from __future__ import annotations

import pandas as pd

from vnedge.data.tape import clean_book, clean_trades


def _trades():
    return pd.DataFrame({
        "ts_ms": [1, 2, 3, 4, 5],
        "price": [100.0, 0.0, 101.0, 102.0, 0.0],
        "amount": [1.0, 1.0, 1.0, 0.0, 1.0],
        "side": ["buy", "sell", "buy", "buy", "sell"],
    })


def test_zero_prices_and_sizes_are_dropped() -> None:
    cleaned, result = clean_trades(_trades())
    assert list(cleaned.price) == [100.0, 101.0]
    assert result.rows_in == 5 and result.rows_out == 2
    assert result.dropped == 3


def test_a_zero_print_would_have_sunk_the_low() -> None:
    """The failure that broke a footprint study: min() collapses to zero."""
    raw = _trades()
    assert raw.price.min() == 0.0
    cleaned, _ = clean_trades(raw)
    assert cleaned.price.min() == 100.0


def test_a_zero_print_would_have_faked_a_long_fill() -> None:
    """The failure that inflated an L2 replay: 0 <= any resting bid."""
    raw = _trades()
    limit = 99.0
    assert (raw.price <= limit).any(), "the corrupt frame reports a touch"
    cleaned, _ = clean_trades(raw)
    assert not (cleaned.price <= limit).any(), "the clean frame does not"


def test_the_drop_count_is_reported_not_hidden() -> None:
    _, result = clean_trades(_trades())
    assert result.dropped_fraction == 0.6


def test_book_rows_need_a_positive_top_of_book() -> None:
    book = pd.DataFrame({"ts_ms": [1, 2, 3, 4],
                         "bid": [100.0, 0.0, 101.0, 102.0],
                         "ask": [100.5, 101.0, 0.0, 101.5]})
    cleaned, result = clean_book(book)
    assert list(cleaned.ts_ms) == [1]
    assert result.dropped == 3


def test_empty_and_columnless_frames_are_safe() -> None:
    empty, result = clean_trades(pd.DataFrame())
    assert len(empty) == 0 and result.dropped == 0
    other = pd.DataFrame({"ts_ms": [1, 2]})
    passed, result = clean_trades(other)
    assert len(passed) == 2 and result.dropped == 0
