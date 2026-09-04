from datetime import UTC, datetime, timedelta

import pytest

from vnedge.strategy.arm_evidence import (
    FrozenPermissionSnapshot,
    ImmutableBarRef,
    freeze_permission_from_row,
)


def test_permission_snapshot_freezes_last_closed_context_and_has_stable_id() -> None:
    row = {
        "timestamp": datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        "mreg_state": "continuation",
        "mreg_h4": "up",
        "mreg_reason": "daily_and_h4_aligned",
    }

    first = freeze_permission_from_row(
        row,
        decision_timeframe="15m",
        context_timeframes=("4h",),
        allow_long=True,
        allow_short=False,
        reason="fallback",
    )
    second = freeze_permission_from_row(
        row,
        decision_timeframe="15m",
        context_timeframes=("4h",),
        allow_long=True,
        allow_short=False,
        reason="fallback",
    )

    assert first.snapshot_id == second.snapshot_id
    assert first.decision_bar.close_time == datetime(2026, 9, 4, 12, 15, tzinfo=UTC)
    assert first.context_bars[0].open_time == datetime(2026, 9, 4, 8, tzinfo=UTC)
    assert first.context_bars[0].close_time == datetime(2026, 9, 4, 12, tzinfo=UTC)
    assert first.regime_state == "continuation"
    assert first.direction == "up"
    assert first.as_dict()["snapshot_id"] == first.snapshot_id


def test_permission_snapshot_rejects_future_or_forming_context() -> None:
    decision = ImmutableBarRef(
        timeframe="15m",
        open_time=datetime(2026, 9, 4, 12, tzinfo=UTC),
        close_time=datetime(2026, 9, 4, 12, 15, tzinfo=UTC),
    )
    future = ImmutableBarRef(
        timeframe="4h",
        open_time=datetime(2026, 9, 4, 12, tzinfo=UTC),
        close_time=datetime(2026, 9, 4, 16, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="future context"):
        FrozenPermissionSnapshot(
            decision_bar=decision,
            context_bars=(future,),
            allow_long=True,
            allow_short=False,
            regime_state="continuation",
            direction="up",
            reason="invalid",
        )
    with pytest.raises(ValueError, match="immutable closed"):
        ImmutableBarRef(
            timeframe="15m",
            open_time=datetime(2026, 9, 4, 12, tzinfo=UTC),
            close_time=datetime(2026, 9, 4, 12, 15, tzinfo=UTC),
            state="forming",  # type: ignore[arg-type]
        )


def test_permission_snapshot_requires_timezone_aware_bar() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ImmutableBarRef(
            timeframe="5m",
            open_time=datetime(2026, 9, 4, 12),  # noqa: DTZ001 - deliberate invalid input
            close_time=datetime(2026, 9, 4, 12)  # noqa: DTZ001 - deliberate invalid input
            + timedelta(minutes=5),
        )
