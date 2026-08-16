"""Edge-investigation kill policy — enforce 'permission to trade, not the code'.

The 2026-08 investigation killed every fast-tick / hourly directional engine and the
retail passive-MM approach (see docs/EDGE_INVESTIGATION_POSTMORTEM_20260816). These
tests keep the kills enforced: a strategy that failed the ladder stays off capital
lanes, no SignalEngine is tradeable, and a NEW engine cannot appear without a recorded
kill/promotion decision. The measurement code stays importable — only trade permission
is removed.
"""

import importlib

from vnedge.strategy import hf_engine_registry as reg
from vnedge.strategy.signal_engine import SignalEngine
from vnedge.strategy.strategy_registry import is_capital_eligible

# Import every engine module so all SignalEngine subclasses are registered for discovery.
_ENGINE_MODULES = (
    "vnedge.strategy.signal_engine",
    "vnedge.strategy.breakout_engine",
    "vnedge.strategy.hourly_range_breakout",
    "vnedge.strategy.current_hour_breakout",
    "vnedge.strategy.mean_reversion_engine",
)
for _m in _ENGINE_MODULES:
    importlib.import_module(_m)


def _all_engine_subclasses() -> set[type]:
    seen: set[type] = set()
    stack = list(SignalEngine.__subclasses__())
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        stack.extend(c.__subclasses__())
    return seen


def test_funding_mr_killed_not_capital_eligible():
    # Forward paper FAILED (2026-08-14); capital permission revoked, shadow still allowed.
    assert not is_capital_eligible("funding_mean_reversion_v1")


def test_non_killed_strategy_still_needs_explicit_capital_approval():
    assert not is_capital_eligible("crypto_trend_atr_margin_v1")


def test_known_hf_engines_are_recorded_killed_and_barred():
    for eid in (
        "OrderFlowImbalanceEngine",
        "ShortTermMeanReversionEngine",
        "HourlyRangeBreakoutEngine",
        "RangeBreakoutEngine",
        "CurrentHourBreakoutEngine",
    ):
        assert eid in reg.KILLED_HF_ENGINES, f"{eid} missing its post-mortem"
        assert reg.killed_reason(eid)
        assert not reg.is_hf_engine_tradeable(eid)


def test_tradeable_allowlist_is_empty_and_no_engine_is_tradeable():
    assert reg.TRADEABLE_HF_ENGINES == frozenset()
    for c in _all_engine_subclasses():
        assert c.tradeable is False, f"{c.__name__} must not be tradeable"


def test_tripwire_every_engine_has_a_kill_or_promotion_decision():
    """A SignalEngine subclass must not appear without a recorded decision. If this
    fails: either the fast-tick world is being re-opened (record the engine in
    hf_engine_registry with its OOS evidence + add to TRADEABLE_HF_ENGINES only after
    a pre-registered pass) or a stray engine slipped in unaudited."""
    for c in _all_engine_subclasses():
        eid = c.engine_id
        assert eid != SignalEngine.engine_id, f"{c.__name__} defines no engine_id"
        assert eid in reg.KILLED_HF_ENGINES or eid in reg.TRADEABLE_HF_ENGINES, (
            f"{c.__name__} (engine_id={eid!r}) has no kill/promotion decision in "
            f"hf_engine_registry — record one before adding a new signal engine."
        )
