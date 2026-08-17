from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.research.avwap_reversal import (
    AVWAPBarObservation,
    AVWAPExitAction,
    AVWAPExitObservation,
    AVWAPInteractionConfig,
    AVWAPInteractionKind,
    classify_avwap_interaction,
    evaluate_avwap_reversal_exit,
)

D = Decimal
START = datetime(2026, 8, 17, tzinfo=UTC)


def bar(
    hour: int,
    close: str,
    *,
    avwap: str | None = "100",
    high: str | None = None,
    low: str | None = None,
    lower_band: str | None = None,
    upper_band: str | None = None,
    quality: str = "ok",
    closed: bool = True,
) -> AVWAPBarObservation:
    close_value = D(close)
    return AVWAPBarObservation(
        as_of=START + timedelta(hours=hour),
        open=close_value,
        high=D(high) if high is not None else close_value,
        low=D(low) if low is not None else close_value,
        close=close_value,
        avwap=D(avwap) if avwap is not None else None,
        lower_band=D(lower_band) if lower_band is not None else None,
        upper_band=D(upper_band) if upper_band is not None else None,
        data_quality=quality,  # type: ignore[arg-type]
        is_closed=closed,
    )


def test_reclaim_loss_and_rejections_use_closed_confirmation():
    config = AVWAPInteractionConfig(touch_tolerance_bps=D(0))
    reclaim = classify_avwap_interaction(
        [bar(0, "99"), bar(1, "101")],
        config,
    )
    loss = classify_avwap_interaction(
        [bar(0, "101"), bar(1, "99")],
        config,
    )
    bull_rejection = classify_avwap_interaction(
        [bar(0, "101"), bar(1, "101", low="99.8", high="101")],
        config,
    )
    bear_rejection = classify_avwap_interaction(
        [bar(0, "99"), bar(1, "99", low="99", high="100.2")],
        config,
    )

    assert reclaim.kind == AVWAPInteractionKind.BULL_RECLAIM
    assert loss.kind == AVWAPInteractionKind.BEAR_LOSS
    assert bull_rejection.kind == AVWAPInteractionKind.BULL_REJECTION
    assert bear_rejection.kind == AVWAPInteractionKind.BEAR_REJECTION
    assert all(
        not result.can_trade and not result.can_promote and result.measurement_only
        for result in (reclaim, loss, bull_rejection, bear_rejection)
    )


def test_failed_reclaim_and_failed_loss_win_over_plain_cross():
    config = AVWAPInteractionConfig(
        touch_tolerance_bps=D(0),
        failed_reclaim_window=3,
    )
    failed_long = classify_avwap_interaction(
        [bar(0, "99"), bar(1, "101"), bar(2, "99")],
        config,
    )
    failed_short = classify_avwap_interaction(
        [bar(0, "101"), bar(1, "99"), bar(2, "101")],
        config,
    )
    assert failed_long.kind == AVWAPInteractionKind.FAILED_BULL_RECLAIM
    assert failed_short.kind == AVWAPInteractionKind.FAILED_BEAR_LOSS


def test_band_reentry_is_descriptive_and_still_targets_avwap_side():
    result = classify_avwap_interaction(
        [
            bar(0, "94", lower_band="95"),
            bar(1, "96", lower_band="95"),
        ],
        AVWAPInteractionConfig(touch_tolerance_bps=D(0)),
    )
    assert result.kind == AVWAPInteractionKind.BULL_BAND_REENTRY
    assert result.distance_bps == D("-400")


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"closed": False}, "forming_bar"),
        ({"quality": "gap"}, "data_quality_gap"),
        ({"avwap": None}, "avwap_unavailable"),
    ],
)
def test_unusable_latest_bar_never_produces_an_interaction(updates, reason):
    latest = bar(1, "101", **updates)
    result = classify_avwap_interaction([bar(0, "99"), latest])
    assert result.kind == AVWAPInteractionKind.UNAVAILABLE
    assert result.reason == reason


def test_long_and_short_reversal_exit_are_strict_closed_bar_mirrors():
    long_exit = evaluate_avwap_reversal_exit(
        AVWAPExitObservation(
            as_of=START,
            side="long",
            close=D("99"),
            avwap=D("100"),
            dual_avwap_bias="between",
        )
    )
    short_exit = evaluate_avwap_reversal_exit(
        AVWAPExitObservation(
            as_of=START,
            side="short",
            close=D("101"),
            avwap=D("100"),
            dual_avwap_bias="between",
        )
    )
    exactly_on_line = evaluate_avwap_reversal_exit(
        AVWAPExitObservation(
            as_of=START,
            side="long",
            close=D("100"),
            avwap=D("100"),
        )
    )

    assert long_exit.action == AVWAPExitAction.EXIT_REVERSAL
    assert short_exit.action == AVWAPExitAction.EXIT_REVERSAL
    assert exactly_on_line.action == AVWAPExitAction.HOLD
    assert long_exit.distance_bps == D("-100")
    assert long_exit.can_trade is False


def test_supporting_dual_bias_prevents_single_line_reversal_exit():
    supported_long = evaluate_avwap_reversal_exit(
        AVWAPExitObservation(
            as_of=START,
            side="long",
            close=D("99"),
            avwap=D("100"),
            dual_avwap_bias="strong_long",
        )
    )
    supported_short = evaluate_avwap_reversal_exit(
        AVWAPExitObservation(
            as_of=START,
            side="short",
            close=D("101"),
            avwap=D("100"),
            dual_avwap_bias="strong_short",
        )
    )
    assert supported_long.action == AVWAPExitAction.HOLD
    assert supported_short.action == AVWAPExitAction.HOLD


def test_missing_or_forming_exit_measurement_is_unavailable():
    missing = evaluate_avwap_reversal_exit(
        AVWAPExitObservation(
            as_of=START,
            side="long",
            close=D("99"),
            avwap=None,
        )
    )
    forming = evaluate_avwap_reversal_exit(
        AVWAPExitObservation(
            as_of=START,
            side="long",
            close=D("99"),
            avwap=D("100"),
            is_closed=False,
        )
    )
    assert missing.action == AVWAPExitAction.UNAVAILABLE
    assert forming.action == AVWAPExitAction.UNAVAILABLE


def test_config_rejects_unregistered_nonsense():
    with pytest.raises(ValueError, match="touch_tolerance_bps"):
        AVWAPInteractionConfig(touch_tolerance_bps=D("NaN"))
    with pytest.raises(ValueError, match="acceptance_bars"):
        AVWAPInteractionConfig(acceptance_bars=1)
