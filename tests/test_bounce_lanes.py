"""The maker/taker pair must differ in exactly one plane."""

from __future__ import annotations

import pytest

from vnedge.strategy.bounce_lanes import (
    LANES,
    MAKER_LANE,
    TAKER_LANE,
    BounceLane,
    lane_by_id,
    maker_lane_at,
)


def test_lanes_share_every_plane_except_entry_and_fee() -> None:
    """A divergence between the lanes is only attributable if nothing else differs."""
    taker, maker = TAKER_LANE.trigger().config, MAKER_LANE.trigger().config
    differing = {
        name
        for name in taker.__dataclass_fields__
        if getattr(taker, name) != getattr(maker, name)
    }
    assert differing == {"entry_mode"}, differing
    assert TAKER_LANE.exits().config == MAKER_LANE.exits().config
    assert TAKER_LANE.arm_source().inner == MAKER_LANE.arm_source().inner


def test_taker_lane_assumes_nothing_about_passive_fills() -> None:
    assert TAKER_LANE.assumes_passive_fill is False
    assert TAKER_LANE.costs().maker_bps is None
    assert MAKER_LANE.assumes_passive_fill is True


def test_a_crossing_entry_cannot_book_a_maker_fee() -> None:
    with pytest.raises(ValueError, match="cannot book a maker fee"):
        BounceLane(lane_id="bad", entry_mode="close", maker_bps=2.0)


def test_taker_pays_taker_on_both_legs_when_held_long() -> None:
    costs = TAKER_LANE.costs()
    assert costs.round_trip_bps(50) == pytest.approx(11.8)
    # the maker lane only saves on the entry leg, and only when filled passively
    maker = MAKER_LANE.costs()
    assert maker.round_trip_bps(50, maker_entry=True) == pytest.approx(7.9)
    assert maker.round_trip_bps(50, maker_entry=False) == pytest.approx(11.8)


def test_lane_lookup_and_fee_sensitivity() -> None:
    assert lane_by_id("structure_bounce_maker") is MAKER_LANE
    with pytest.raises(KeyError):
        lane_by_id("nope")
    sensitive = maker_lane_at(4.0)
    assert sensitive.maker_bps == 4.0
    assert sensitive.entry_mode == MAKER_LANE.entry_mode


def test_every_lane_is_measurement_only() -> None:
    from vnedge.strategy.strategy_registry import is_capital_eligible

    for lane in LANES:
        assert not is_capital_eligible(lane.strategy_id), lane.lane_id
