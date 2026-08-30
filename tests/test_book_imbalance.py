from datetime import UTC, datetime, timedelta

import pytest

from vnedge.exchange.book_imbalance import (
    BookTape,
    imbalance_allows,
    imbalance_l1,
    imbalance_l2,
    multilevel_microprice,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def test_l1_bid_heavy_book_allows_long_and_blocks_short() -> None:
    book = imbalance_l1(100.0, 100.5, 70.0, 30.0, tick=0.5, ts=NOW)
    assert book is not None
    assert book.imb == pytest.approx(0.4)
    assert book.microprice == pytest.approx(100.35)
    assert book.spread_ticks == pytest.approx(1.0)
    assert imbalance_allows("long", book, min_abs=0.2, max_spread_ticks=2) is None
    assert imbalance_allows("short", book, min_abs=0.2, max_spread_ticks=2) == (
        "imb_not_ask_heavy"
    )


def test_book_tape_does_not_become_live_from_elapsed_transport_time() -> None:
    book = imbalance_l1(100.0, 100.5, 70.0, 30.0, tick=0.5, ts=NOW)
    assert book is not None
    tape = BookTape(stale_after_s=2.0)
    tape.on_book(book)
    assert tape.live(NOW + timedelta(seconds=2)) is book
    # A heartbeat would update transport elsewhere; the untouched book tape
    # remains stale and therefore cannot complete acceptance.
    assert tape.live(NOW + timedelta(seconds=3)) is None
    assert imbalance_allows("long", None, min_abs=0.2, max_spread_ticks=2) == (
        "book_stale"
    )


def test_wide_spread_rejects_even_extreme_imbalance() -> None:
    book = imbalance_l1(100.0, 101.5, 99.0, 1.0, tick=0.5, ts=NOW)
    assert book is not None
    assert book.spread_ticks == 3.0
    assert imbalance_allows("long", book, min_abs=0.2, max_spread_ticks=2) == (
        "spread_too_wide"
    )


def test_zero_depth_is_invalid_and_l2_aggregates_contract_sizes() -> None:
    assert imbalance_l1(100.0, 100.5, 0.0, 30.0, tick=0.5, ts=NOW) is None
    assert imbalance_l1(100.0, 100.0, 10.0, 10.0, tick=0.5, ts=NOW) is None
    assert imbalance_l1(100.5, 100.0, 10.0, 10.0, tick=0.5, ts=NOW) is None
    book = imbalance_l2(
        [(100.0, 40.0), (99.5, 30.0)],
        [(100.5, 20.0), (101.0, 10.0)],
        tick=0.5,
        ts=NOW,
        levels=2,
        decay_k=0.0,
    )
    assert book is not None
    assert book.bid_size == 70.0
    assert book.ask_size == 30.0
    assert book.levels == 2


def test_multilevel_equal_book_is_mid() -> None:
    micro = multilevel_microprice(
        [(100.0, 10.0), (99.5, 10.0)],
        [(100.5, 10.0), (101.0, 10.0)],
        tick=0.5,
        ts=NOW,
        levels=2,
        decay_k=0.0,
    )
    assert micro is not None
    assert micro.microprice == pytest.approx(100.25)
    assert micro.imb == pytest.approx(0.0)
    assert micro.levels_used == 2


def test_multilevel_bid_heavy_book_pulls_microprice_toward_ask() -> None:
    micro = multilevel_microprice(
        [(100.0, 40.0), (99.5, 20.0)],
        [(100.5, 10.0), (101.0, 5.0)],
        tick=0.5,
        ts=NOW,
        levels=2,
        decay_k=0.35,
    )
    assert micro is not None
    assert micro.microprice > micro.mid
    assert micro.imb > 0
    assert micro.bid <= micro.microprice <= micro.ask


def test_multilevel_crossed_and_locked_books_are_invalid() -> None:
    assert multilevel_microprice(
        [(101.0, 1.0)],
        [(100.0, 1.0)],
        tick=0.5,
        ts=NOW,
    ) is None
    assert multilevel_microprice(
        [(100.0, 1.0)],
        [(100.0, 1.0)],
        tick=0.5,
        ts=NOW,
    ) is None


def test_multilevel_decay_reduces_far_size_influence() -> None:
    far = multilevel_microprice(
        [(100.0, 1.0), (98.0, 1_000.0)],
        [(100.5, 1.0)],
        tick=0.5,
        ts=NOW,
        levels=5,
        decay_k=0.35,
    )
    raw = multilevel_microprice(
        [(100.0, 1.0), (98.0, 1_000.0)],
        [(100.5, 1.0)],
        tick=0.5,
        ts=NOW,
        levels=5,
        decay_k=0.0,
    )
    assert far is not None and raw is not None
    assert abs(far.imb) < abs(raw.imb)
