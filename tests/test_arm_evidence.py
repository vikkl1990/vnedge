from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.strategy.arm_evidence import (
    FrozenPermissionSnapshot,
    ImmutableBarRef,
    MissingHtfContext,
    freeze_permission_from_bound_frames,
    freeze_permission_from_row,
    last_eligible_context_bar,
)


def _closed_row(timestamp: str, *, source: str = "canonical_tick_lake") -> dict:
    return {
        "timestamp": pd.Timestamp(timestamp),
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 10.0,
        "is_closed": True,
        "data_quality": "ok",
        "candle_source": source,
    }


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


def test_permission_snapshot_uses_actual_bound_context_not_calendar_floor() -> None:
    decision = {
        "timestamp": datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        "open": 100,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 10,
        "is_closed": True,
        "data_quality": "ok",
        "candle_source": "canonical_tick_lake",
    }
    # The calendar-implied 08:00-12:00 bar is absent. The permission machine
    # actually consumed 04:00-08:00, and evidence must say exactly that.
    context = {
        "timestamp": datetime(2026, 9, 4, 4, 0, tzinfo=UTC),
        "open": 98,
        "high": 103,
        "low": 97,
        "close": 101,
        "volume": 80,
        "is_closed": True,
        "data_quality": "ok",
        "candle_source": "canonical_tick_lake",
    }

    snapshot = freeze_permission_from_row(
        decision,
        decision_timeframe="15m",
        context_timeframes=("4h",),
        allow_long=True,
        allow_short=False,
        reason="bound",
        context_rows={"4h": context},
        regime_version="regime_v2",
        require_bound_context=True,
        require_closed_truth=True,
    )

    assert snapshot.context_bars[0].open_time == datetime(2026, 9, 4, 4, tzinfo=UTC)
    assert snapshot.context_bars[0].source == "canonical_tick_lake"
    assert snapshot.context_bars[0].content_sha256 is not None
    assert snapshot.regime_version == "regime_v2"


def test_permission_snapshot_refuses_missing_required_bound_context() -> None:
    row = {
        "timestamp": datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        "is_closed": True,
        "data_quality": "ok",
        "candle_source": "canonical_tick_lake",
    }
    with pytest.raises(ValueError, match="required bound context row is missing: 4h"):
        freeze_permission_from_row(
            row,
            decision_timeframe="15m",
            context_timeframes=("4h",),
            allow_long=True,
            allow_short=False,
            reason="missing",
            context_rows={},
            regime_version="regime_v2",
            require_bound_context=True,
            require_closed_truth=True,
        )


def test_permission_snapshot_refuses_untrusted_strict_source() -> None:
    row = {
        "timestamp": datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        "is_closed": True,
        "data_quality": "ok",
        "candle_source": "exchange_ohlcv",
    }
    with pytest.raises(ValueError, match="untrusted permission candle source"):
        freeze_permission_from_row(
            row,
            decision_timeframe="15m",
            context_timeframes=(),
            allow_long=True,
            allow_short=False,
            reason="untrusted",
            regime_version="regime_v2",
            require_closed_truth=True,
        )


def test_permission_snapshot_requires_version_in_strict_mode() -> None:
    row = {
        "timestamp": datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        "is_closed": True,
        "data_quality": "ok",
        "candle_source": "canonical_tick_lake",
    }
    with pytest.raises(ValueError, match="requires a regime version"):
        freeze_permission_from_row(
            row,
            decision_timeframe="15m",
            context_timeframes=(),
            allow_long=True,
            allow_short=False,
            reason="unversioned",
            require_closed_truth=True,
        )


def test_snapshot_id_is_stable_across_equivalent_scalar_representations() -> None:
    common = {
        "timestamp": datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 10,
        "is_closed": True,
        "data_quality": "ok",
        "candle_source": "canonical_tick_lake",
    }
    integer_row = {**common, "open": 100, "trade_count": 3}
    decimal_row = {**common, "open": 100.0, "trade_count": 3.0}

    first = freeze_permission_from_row(
        integer_row,
        decision_timeframe="15m",
        context_timeframes=(),
        allow_long=True,
        allow_short=False,
        reason="stable",
        regime_version="regime_v2",
        require_closed_truth=True,
    )
    second = freeze_permission_from_row(
        decimal_row,
        decision_timeframe="15m",
        context_timeframes=(),
        allow_long=True,
        allow_short=False,
        reason="stable",
        regime_version="regime_v2",
        require_closed_truth=True,
    )

    assert first.decision_bar.content_sha256 == second.decision_bar.content_sha256
    assert first.snapshot_id == second.snapshot_id


def test_permission_snapshot_roundtrip_rejects_tampered_payload() -> None:
    snapshot = freeze_permission_from_row(
        {
            "timestamp": datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 10,
            "is_closed": True,
            "data_quality": "ok",
            "candle_source": "router",
        },
        decision_timeframe="15m",
        context_timeframes=(),
        allow_long=True,
        allow_short=False,
        reason="roundtrip",
        regime_version="regime_v2",
        require_closed_truth=True,
    )
    payload = snapshot.as_dict()
    assert FrozenPermissionSnapshot.from_dict(payload) == snapshot

    payload["reason"] = "tampered"
    with pytest.raises(ValueError, match="snapshot_id does not match"):
        FrozenPermissionSnapshot.from_dict(payload)


def test_bound_freeze_rejects_empty_required_context() -> None:
    with pytest.raises(MissingHtfContext, match=r"htf_context_missing:4h:absent"):
        freeze_permission_from_bound_frames(
            _closed_row("2026-09-04T12:00:00Z"),
            decision_timeframe="15m",
            context_frames={"4h": pd.DataFrame()},
            context_health={"4h": True},
            required_context=("4h",),
            allow_long=True,
            allow_short=False,
            reason="test",
            regime_version="regime_v2",
        )


def test_bound_freeze_rejects_calendar_floor_when_actual_bar_is_future() -> None:
    future_only = pd.DataFrame([_closed_row("2026-09-04T12:00:00Z")])
    with pytest.raises(MissingHtfContext, match=r"htf_context_missing:4h:absent"):
        freeze_permission_from_bound_frames(
            _closed_row("2026-09-04T12:00:00Z"),
            decision_timeframe="15m",
            context_frames={"4h": future_only},
            context_health={"4h": True},
            required_context=("4h",),
            allow_long=True,
            allow_short=False,
            reason="test",
            regime_version="regime_v2",
        )


def test_bound_freeze_uses_exact_last_eligible_4h_and_daily_rows() -> None:
    h4 = pd.DataFrame(
        [
            _closed_row("2026-09-04T04:00:00Z"),
            _closed_row("2026-09-04T08:00:00Z"),
            _closed_row("2026-09-04T12:00:00Z"),
        ]
    )
    daily = pd.DataFrame(
        [
            _closed_row("2026-09-02T00:00:00Z", source="router"),
            _closed_row("2026-09-03T00:00:00Z", source="router"),
        ]
    )
    decision = _closed_row("2026-09-04T12:00:00Z", source="router")

    snapshot = freeze_permission_from_bound_frames(
        decision,
        decision_timeframe="15m",
        context_frames={"4h": h4, "1d": daily},
        context_health={"4h": True, "1d": True},
        required_context=("4h", "1d"),
        allow_long=True,
        allow_short=False,
        reason="test",
        regime_version="regime_v2",
    )

    assert [bar.open_time for bar in snapshot.context_bars] == [
        datetime(2026, 9, 4, 8, tzinfo=UTC),
        datetime(2026, 9, 3, 0, tzinfo=UTC),
    ]
    assert last_eligible_context_bar(
        h4,
        timeframe="4h",
        decision_close=datetime(2026, 9, 4, 12, 15, tzinfo=UTC),
    ) == snapshot.context_bars[0]
