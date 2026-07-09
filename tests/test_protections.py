"""Entry-protection state machine + engine parity.

The protections module is a pure state machine (risk/protections.py) that
both engines consult identically: cooldown after STOP exits only, and a
stop-window guard. Defaults are OFF — zero behavior change unless a config
explicitly enables a protection.
"""

import pytest
from pydantic import ValidationError

from vnedge.risk.protections import ProtectionConfig, ProtectionState


def state(**kwargs) -> ProtectionState:
    return ProtectionState(ProtectionConfig(**kwargs))


# --- Defaults: everything OFF ------------------------------------------------------


def test_defaults_are_off():
    cfg = ProtectionConfig()
    assert cfg.cooldown_bars_after_stop == 0
    assert cfg.max_stops_per_window == 0
    assert cfg.stop_window_bars == 0
    assert not cfg.enabled


def test_default_config_never_blocks_even_after_stops():
    s = state()
    for bar in range(10):
        s.on_exit("stop", bar)
        allowed, reason = s.entries_allowed(bar)
        assert allowed and reason is None


# --- Cooldown after stop -----------------------------------------------------------


def test_cooldown_blocks_same_bar_reentry():
    s = state(cooldown_bars_after_stop=1)
    s.on_exit("stop", 5)
    allowed, reason = s.entries_allowed(5)
    assert not allowed
    assert reason == "post_exit_cooldown: 1 bar(s) remaining"
    assert s.entries_allowed(6) == (True, None)


def test_cooldown_counts_down_across_bars():
    s = state(cooldown_bars_after_stop=3)
    s.on_exit("stop", 10)
    assert s.entries_allowed(10) == (False, "post_exit_cooldown: 3 bar(s) remaining")
    assert s.entries_allowed(11) == (False, "post_exit_cooldown: 2 bar(s) remaining")
    assert s.entries_allowed(12) == (False, "post_exit_cooldown: 1 bar(s) remaining")
    assert s.entries_allowed(13) == (True, None)


def test_cooldown_applies_only_to_stop_exits():
    # Refinement over the original any-exit cooldown: a winner closing is not
    # evidence the entry condition went bad.
    s = state(cooldown_bars_after_stop=2)
    for reason in ("take_profit", "max_holding", "end_of_data"):
        s.on_exit(reason, 5)
        assert s.entries_allowed(5) == (True, None)


def test_tick_stop_counts_as_stop():
    s = state(cooldown_bars_after_stop=1)
    s.on_exit("tick_stop", 7)
    allowed, _ = s.entries_allowed(7)
    assert not allowed


def test_overlapping_stops_extend_not_shrink_cooldown():
    s = state(cooldown_bars_after_stop=3)
    s.on_exit("stop", 10)  # blocks 10, 11, 12
    s.on_exit("stop", 11)  # blocks through 13
    assert s.entries_allowed(13)[0] is False
    assert s.entries_allowed(14)[0] is True


# --- Stop-window guard -------------------------------------------------------------


def test_window_guard_blocks_at_max_stops():
    s = state(max_stops_per_window=2, stop_window_bars=8)
    s.on_exit("stop", 5)
    assert s.entries_allowed(5) == (True, None)  # one stop: under the limit
    s.on_exit("stop", 9)
    allowed, reason = s.entries_allowed(9)
    assert not allowed
    assert reason == "stop_window_guard: 2 stops in last 8 bars (max 2)"


def test_window_guard_releases_when_stops_age_out():
    s = state(max_stops_per_window=2, stop_window_bars=8)
    s.on_exit("stop", 5)
    s.on_exit("stop", 9)
    assert s.entries_allowed(12)[0] is False  # (4, 12] holds both stops
    assert s.entries_allowed(13)[0] is True   # (5, 13] holds only bar 9


def test_window_guard_ignores_non_stop_exits():
    s = state(max_stops_per_window=1, stop_window_bars=100)
    s.on_exit("take_profit", 5)
    s.on_exit("max_holding", 6)
    assert s.entries_allowed(7) == (True, None)
    s.on_exit("stop", 8)
    assert s.entries_allowed(8)[0] is False


def test_combined_block_reports_every_active_reason():
    s = state(cooldown_bars_after_stop=2, max_stops_per_window=1, stop_window_bars=8)
    s.on_exit("stop", 5)
    allowed, reason = s.entries_allowed(5)
    assert not allowed
    assert reason == (
        "post_exit_cooldown: 2 bar(s) remaining; "
        "stop_window_guard: 1 stops in last 8 bars (max 1)"
    )


def test_one_sided_window_guard_config_rejected():
    with pytest.raises(ValidationError):
        ProtectionConfig(max_stops_per_window=2)  # window bars missing
    with pytest.raises(ValidationError):
        ProtectionConfig(stop_window_bars=8)  # max stops missing


def test_config_is_frozen():
    cfg = ProtectionConfig(cooldown_bars_after_stop=1)
    with pytest.raises(ValidationError):
        cfg.cooldown_bars_after_stop = 5


def test_on_exit_never_raises_while_blocked():
    # Exits are NEVER affected by protections — recording an exit while
    # entries are blocked must work unconditionally (reduce-only invariant).
    s = state(cooldown_bars_after_stop=5, max_stops_per_window=1, stop_window_bars=10)
    s.on_exit("stop", 5)
    assert s.entries_allowed(6)[0] is False
    s.on_exit("stop", 6)  # another exit while blocked: fine
    s.on_exit("take_profit", 7)
    assert s.entries_allowed(7)[0] is False
