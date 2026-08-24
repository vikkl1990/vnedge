"""One cost model. A lane may not hold a private fee assumption.

Spec P0 2.3: the same trade must price identically wherever it is priced.
Before this, fee constants lived in a dozen modules with values that
disagreed -- 5.0, 5.9, 11.8, 12.0 -- so a strategy's verdict depended on
which module happened to charge it.
"""

from __future__ import annotations

import pytest

from vnedge.plan.cost_model import CostModel
from vnedge.runtime.scanner_session import SessionCosts


def test_session_costs_agree_with_the_canonical_model() -> None:
    """A scanner session and the plan gate must charge the same trade alike."""
    model = CostModel.for_profile("delta_scalp")
    costs = SessionCosts.from_profile("delta_scalp")
    for held_bars in (0, 1, 6, 7, 12, 24, 60):
        hold_minutes = held_bars * 5.0
        assert costs.round_trip_bps(held_bars) == pytest.approx(
            model.round_trip_bps(hold_minutes=hold_minutes, include_safety=False)
        ), held_bars


def test_delta_swing_session_and_plan_costs_have_gst_parity() -> None:
    model = CostModel.for_profile("delta_swing")
    costs = SessionCosts.from_profile("delta_swing", bar_minutes=15.0)
    for held_bars in (0, 1, 2, 8, 48):
        assert costs.round_trip_bps(held_bars) == pytest.approx(
            model.round_trip_bps(
                hold_minutes=held_bars * 15.0,
                include_safety=False,
            )
        )
    assert costs.round_trip_bps(48) == pytest.approx(15.8)


def test_unverified_scalper_offer_is_not_silently_recast_as_maker() -> None:
    """A close-fee waiver is neither a maker exit nor a generic entitlement."""
    model = CostModel.for_profile("delta_scalp")
    costs = SessionCosts.from_profile(
        "delta_scalp", free_close_within_bars=6
    )
    for held_bars in (1, 6, 7):
        assert costs.round_trip_bps(held_bars) == pytest.approx(
            model.round_trip_bps(
                hold_minutes=held_bars * 5.0,
                include_safety=False,
            )
        )


def test_the_hardcoded_5_9_is_the_delta_taker_leg() -> None:
    """5.9 was copied into three modules; it is 5.0 x 1.18 GST, not a magic number."""
    model = CostModel.for_profile("delta_scalp")
    assert model.fee_bps() * model.config.fee_gst_mult == pytest.approx(5.9)


def test_the_safety_buffer_is_a_gate_margin_not_a_realized_cost() -> None:
    """Charging it to realized PnL would invent a cost the venue never bills."""
    model = CostModel.for_profile("delta_scalp")
    costs = SessionCosts.from_profile("delta_scalp")
    gate = model.round_trip_bps(hold_minutes=300)
    realized = costs.round_trip_bps(60)
    assert gate - realized == pytest.approx(model.config.safety_buffer_bps)


def test_canonical_costs_exceed_fee_only_costs() -> None:
    """Fee-only omits SLIPPAGE -- it flatters every result.

    On delta_scalp that is 6 bps of round trip, comparable to the entire edge
    of everything measured so far.
    """
    legacy = SessionCosts()
    canonical = SessionCosts.from_profile("delta_scalp")
    for held_bars in (12, 60):
        assert canonical.round_trip_bps(held_bars) > legacy.round_trip_bps(held_bars) + 4


def test_a_maker_entry_is_cheaper_but_not_free() -> None:
    canonical = SessionCosts.from_profile("delta_scalp")
    taker = canonical.round_trip_bps(60, maker_entry=False)
    maker = canonical.round_trip_bps(60, maker_entry=True)
    assert maker < taker, "a passive entry must cost less than crossing"
    # slippage and safety still apply: passive entry is not a free trade
    assert maker > 10.0


def test_unknown_profile_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unknown cost profile"):
        SessionCosts.from_profile("no_such_profile")


def test_funding_is_charged_per_completed_8h_period() -> None:
    """Immaterial at 1h holds, decisive at multi-day ones.

    Every arm measured before 2026-08-21 held for about an hour and paid no
    funding. A trend arm holding five days crosses fifteen stamps, which at a
    routine rate is comparable to an entire round trip.
    """
    costs = SessionCosts.from_profile(
        "delta_scalp", free_close_within_bars=0, bar_minutes=240.0,
        funding_bps_per_8h=1.0,
    )
    assert costs.funding_bps(1) == 0.0        # 4h: no completed period
    assert costs.funding_bps(2) == pytest.approx(1.0)    # 8h
    assert costs.funding_bps(30) == pytest.approx(15.0)  # 5 days
    flat = SessionCosts.from_profile("delta_scalp", bar_minutes=240.0)
    assert flat.funding_bps(30) == 0.0, "funding must default OFF"
    assert costs.round_trip_bps(30) > flat.round_trip_bps(30) + 14


def test_a_position_closed_before_the_stamp_pays_nothing() -> None:
    costs = SessionCosts(funding_bps_per_8h=1.0, bar_minutes=60.0)
    assert costs.funding_bps(7) == 0.0
    assert costs.funding_bps(8) == pytest.approx(1.0)
    assert costs.funding_bps(9) == pytest.approx(1.0)
