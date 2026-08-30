from decimal import Decimal

import pytest

from vnedge.exchange.delta_price_grid import (
    format_delta_price,
    is_on_tick,
    snap_limit_entry,
    snap_protective_stop,
    snap_take_profit,
)


def test_delta_tick_grid_is_exact_decimal_not_binary_float():
    assert is_on_tick("110000.5", "0.5")
    assert not is_on_tick("110000.25", "0.5")
    assert is_on_tick("4200.05", "0.05")
    assert format_delta_price(Decimal("110000.5000")) == "110000.5"


def test_entry_rounding_respects_fill_vs_passive_direction():
    assert snap_limit_entry(
        side="buy", price="110000.37", tick="0.5", post_only=False
    ) == Decimal("110000.5")
    assert snap_limit_entry(
        side="buy", price="110000.37", tick="0.5", post_only=True
    ) == Decimal("110000.0")
    assert snap_limit_entry(
        side="sell", price="110000.37", tick="0.5", post_only=False
    ) == Decimal("110000.0")
    assert snap_limit_entry(
        side="sell", price="110000.37", tick="0.5", post_only=True
    ) == Decimal("110000.5")


def test_protection_never_rounds_away_from_danger():
    assert snap_protective_stop(
        position_side="long", price="110000.37", tick="0.5"
    ) == Decimal("110000.0")
    assert snap_protective_stop(
        position_side="short", price="110000.37", tick="0.5"
    ) == Decimal("110000.5")
    assert snap_take_profit(
        position_side="long", price="110000.37", tick="0.5"
    ) == Decimal("110000.5")
    assert snap_take_profit(
        position_side="short", price="110000.37", tick="0.5"
    ) == Decimal("110000.0")


def test_invalid_tick_inputs_fail_closed():
    with pytest.raises(ValueError):
        is_on_tick("100", "0")
    with pytest.raises(ValueError):
        snap_limit_entry(side="flat", price="100", tick="0.5", post_only=False)
