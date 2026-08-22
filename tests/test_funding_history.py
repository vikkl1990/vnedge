"""Funding is signed. Getting the sign wrong flips a cost into income."""

from __future__ import annotations

import pytest

from research.funding_history import funding_cost_bps

# three stamps at +1 bp, +2 bps, -0.5 bps
STAMPS = [(1_000, 0.0001), (2_000, 0.0002), (3_000, -0.00005)]


def test_a_long_pays_a_positive_rate() -> None:
    assert funding_cost_bps(STAMPS, "long", 0, 4_000) == pytest.approx(2.5)


def test_a_short_receives_it() -> None:
    """The same window, opposite side, opposite sign -- not the same cost."""
    assert funding_cost_bps(STAMPS, "short", 0, 4_000) == pytest.approx(-2.5)


def test_only_stamps_inside_the_hold_are_charged() -> None:
    assert funding_cost_bps(STAMPS, "long", 1_000, 2_000) == pytest.approx(2.0)
    assert funding_cost_bps(STAMPS, "long", 0, 1_000) == pytest.approx(1.0)
    assert funding_cost_bps(STAMPS, "long", 3_000, 9_000) == 0.0


def test_the_entry_stamp_is_excluded_and_the_exit_stamp_included() -> None:
    """A position opened exactly on a stamp has not held through it."""
    assert funding_cost_bps(STAMPS, "long", 1_000, 3_000) == pytest.approx(1.5)
