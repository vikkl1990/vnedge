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
    for held_bars in (12, 24, 60):
        hold_minutes = held_bars * 5.0
        assert costs.round_trip_bps(held_bars) == pytest.approx(
            model.round_trip_bps(hold_minutes=hold_minutes)
        ), held_bars


def test_the_hardcoded_5_9_is_the_delta_taker_leg() -> None:
    """5.9 was copied into three modules; it is 5.0 x 1.18 GST, not a magic number."""
    model = CostModel.for_profile("delta_scalp")
    assert model.fee_bps() * model.config.fee_gst_mult == pytest.approx(5.9)


def test_canonical_costs_exceed_fee_only_costs() -> None:
    """Fee-only omits slippage and the safety buffer -- it flatters every result.

    This is not a rounding difference: on delta_scalp it is 8 bps of round
    trip, which is comparable to the entire edge of everything measured so far.
    """
    legacy = SessionCosts()
    canonical = SessionCosts.from_profile("delta_scalp")
    for held_bars in (12, 60):
        assert canonical.round_trip_bps(held_bars) > legacy.round_trip_bps(held_bars) + 5


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
