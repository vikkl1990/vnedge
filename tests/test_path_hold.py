from datetime import UTC, datetime
from decimal import Decimal

import pytest

from vnedge.data.structure import StructureEventType, StructureTrend
from vnedge.research.path_hold import (
    PathAction,
    PathHoldConfig,
    PathObservation,
    evaluate_path_hold,
)


def observation(**overrides) -> PathObservation:
    values = {
        "as_of": datetime(2026, 8, 17, 12, tzinfo=UTC),
        "side": "long",
        "price": Decimal(100),
        "next_target": Decimal(104),
        "active_stop": Decimal(98),
        "atr": Decimal(2),
        "htf_trend": StructureTrend.UP,
        "ltf_trend": StructureTrend.UP,
    }
    values.update(overrides)
    return PathObservation(**values)


def test_holds_only_when_closed_causal_path_is_valid():
    decision = evaluate_path_hold(
        observation(hit_probability=Decimal("0.61")),
        PathHoldConfig(
            max_target_distance_atr=Decimal(2),
            min_hit_probability=Decimal("0.60"),
        ),
    )
    assert decision.action == PathAction.HOLD
    assert decision.continue_hold is True
    assert decision.target_distance_atr == Decimal(2)
    assert decision.stop_distance_atr == Decimal(1)


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (StructureEventType.BOS_DOWN, "opposing_bos_down"),
        (StructureEventType.CHOCH_DOWN, "opposing_choch_down"),
    ],
)
def test_opposing_closed_structure_exits_before_reachability(event, reason):
    decision = evaluate_path_hold(
        observation(
            structure_event=event,
            hit_probability=Decimal("0.99"),
        ),
        PathHoldConfig(
            max_target_distance_atr=Decimal(10),
            min_hit_probability=Decimal("0.50"),
        ),
    )
    assert decision.action == PathAction.EXIT_REVERSAL
    assert decision.continue_hold is False
    assert decision.reason == reason


def test_target_beyond_frozen_atr_limit_is_not_held():
    decision = evaluate_path_hold(
        observation(next_target=Decimal("106.01")),
        PathHoldConfig(max_target_distance_atr=Decimal(3)),
    )
    assert decision.action == PathAction.EXIT_UNREACHABLE
    assert decision.reason == "target_beyond_frozen_atr_limit"


def test_probability_threshold_is_optional_but_fail_closed_when_enabled():
    config = PathHoldConfig(
        max_target_distance_atr=Decimal(3),
        min_hit_probability=Decimal("0.60"),
    )
    missing = evaluate_path_hold(observation(), config)
    low = evaluate_path_hold(
        observation(hit_probability=Decimal("0.59")),
        config,
    )
    assert missing.action == PathAction.EXIT_UNAVAILABLE
    assert missing.reason == "hit_probability_unavailable"
    assert low.action == PathAction.EXIT_UNREACHABLE


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"is_closed": False}, "forming_bar"),
        ({"data_quality": "gap"}, "data_quality_gap"),
        ({"as_of": datetime(2026, 8, 17, 12)}, "naive_as_of"),  # noqa: DTZ001
    ],
)
def test_unusable_measurements_never_grant_hold(updates, reason):
    decision = evaluate_path_hold(
        observation(**updates),
        PathHoldConfig(max_target_distance_atr=Decimal(3)),
    )
    assert decision.action == PathAction.EXIT_UNAVAILABLE
    assert decision.continue_hold is False
    assert decision.reason == reason


def test_invalid_optional_probability_never_leaks_into_hold():
    decision = evaluate_path_hold(
        observation(hit_probability=Decimal("NaN")),
        PathHoldConfig(max_target_distance_atr=Decimal(3)),
    )
    assert decision.action == PathAction.EXIT_UNAVAILABLE
    assert decision.reason == "invalid_hit_probability"


def test_short_path_is_exact_directional_mirror():
    config = PathHoldConfig(max_target_distance_atr=Decimal(2))
    valid = evaluate_path_hold(
        observation(
            side="short",
            next_target=Decimal(96),
            active_stop=Decimal(102),
            htf_trend=StructureTrend.DOWN,
            ltf_trend=StructureTrend.DOWN,
        ),
        config,
    )
    reversal = evaluate_path_hold(
        observation(
            side="short",
            next_target=Decimal(96),
            active_stop=Decimal(102),
            htf_trend=StructureTrend.DOWN,
            ltf_trend=StructureTrend.DOWN,
            structure_event=StructureEventType.CHOCH_UP,
        ),
        config,
    )
    assert valid.action == PathAction.HOLD
    assert reversal.action == PathAction.EXIT_REVERSAL


def test_config_rejects_unregistered_nonsense():
    with pytest.raises(ValueError, match="max_target_distance_atr"):
        PathHoldConfig(max_target_distance_atr=Decimal("NaN"))
    with pytest.raises(ValueError, match="min_hit_probability"):
        PathHoldConfig(
            max_target_distance_atr=Decimal(3),
            min_hit_probability=Decimal("1.01"),
        )


def test_avwap_reversal_exit_is_opt_in_and_research_only():
    below = observation(
        price=Decimal(99),
        anchor_avwap=Decimal(100),
        dual_avwap_bias="between",
    )
    legacy = evaluate_path_hold(
        below,
        PathHoldConfig(max_target_distance_atr=Decimal(3)),
    )
    enabled = evaluate_path_hold(
        below,
        PathHoldConfig(
            max_target_distance_atr=Decimal(3),
            exit_on_avwap_reversal=True,
        ),
    )

    assert legacy.action == PathAction.HOLD
    assert enabled.action == PathAction.EXIT_REVERSAL
    assert enabled.reason == "long_closed_below_avwap_without_strong_long_bias"


def test_opted_in_avwap_exit_fails_closed_when_line_is_missing():
    decision = evaluate_path_hold(
        observation(anchor_avwap=None),
        PathHoldConfig(
            max_target_distance_atr=Decimal(3),
            exit_on_avwap_reversal=True,
        ),
    )
    assert decision.action == PathAction.EXIT_UNAVAILABLE
    assert decision.reason == "avwap_unavailable"


def test_strong_dual_bias_can_preserve_path_after_single_line_loss():
    decision = evaluate_path_hold(
        observation(
            price=Decimal(99),
            anchor_avwap=Decimal(100),
            dual_avwap_bias="strong_long",
        ),
        PathHoldConfig(
            max_target_distance_atr=Decimal(3),
            exit_on_avwap_reversal=True,
        ),
    )
    assert decision.action == PathAction.HOLD
