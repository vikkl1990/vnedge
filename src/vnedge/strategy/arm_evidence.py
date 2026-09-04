"""Immutable evidence attached to a quote-triggered scanner arm.

An arm is created from one closed decision bar plus the last *closed* context
bars available at that instant.  Quotes may later accept the arm, but they may
not silently replace its higher-timeframe permission.  This small snapshot is
therefore journaled with transitions, intents, and outcomes so replay can prove
which bars authorized a decision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from vnedge.data.candles import TF_SECONDS, BarState, floor_time


def _utc_datetime(value: object) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()  # type: ignore[union-attr]
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError("arm evidence timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("arm evidence timestamp must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ImmutableBarRef:
    timeframe: str
    open_time: datetime
    close_time: datetime
    version: int = 1
    state: Literal["closed_immutable"] = "closed_immutable"

    def __post_init__(self) -> None:
        try:
            seconds = TF_SECONDS[self.timeframe]
        except KeyError as exc:
            raise ValueError(f"unsupported evidence timeframe: {self.timeframe!r}") from exc
        open_time = _utc_datetime(self.open_time)
        close_time = _utc_datetime(self.close_time)
        if open_time != floor_time(open_time, self.timeframe):
            raise ValueError("evidence bar open_time is not timeframe-aligned")
        if close_time != open_time + timedelta(seconds=seconds):
            raise ValueError("evidence bar close_time is not one complete timeframe")
        if self.version != 1:
            raise ValueError("evidence bar version must be 1")
        if self.state != BarState.CLOSED_IMMUTABLE.value:
            raise ValueError("an arm may reference immutable closed bars only")
        object.__setattr__(self, "open_time", open_time)
        object.__setattr__(self, "close_time", close_time)

    def as_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "version": self.version,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class FrozenPermissionSnapshot:
    decision_bar: ImmutableBarRef
    context_bars: tuple[ImmutableBarRef, ...]
    allow_long: bool
    allow_short: bool
    regime_state: str
    direction: str
    reason: str

    def __post_init__(self) -> None:
        if not (self.allow_long or self.allow_short):
            raise ValueError("permission snapshot must allow at least one side")
        if any(bar.close_time > self.decision_bar.close_time for bar in self.context_bars):
            raise ValueError("permission snapshot cannot reference future context")
        if len({bar.timeframe for bar in self.context_bars}) != len(self.context_bars):
            raise ValueError("permission snapshot context timeframes must be unique")

    @property
    def snapshot_id(self) -> str:
        encoded = json.dumps(self.as_dict(include_id=False), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    def as_dict(self, *, include_id: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "decision_bar": self.decision_bar.as_dict(),
            "context_bars": [bar.as_dict() for bar in self.context_bars],
            "allow_long": self.allow_long,
            "allow_short": self.allow_short,
            "regime_state": self.regime_state,
            "direction": self.direction,
            "reason": self.reason,
        }
        if include_id:
            payload["snapshot_id"] = self.snapshot_id
        return payload


def _row_text(row: Mapping[str, Any], names: Sequence[str], default: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() and str(value).lower() != "nan":
            return str(value)
    return default


def freeze_permission_from_row(
    row: Mapping[str, Any],
    *,
    decision_timeframe: str,
    context_timeframes: Sequence[str],
    allow_long: bool,
    allow_short: bool,
    reason: str,
) -> FrozenPermissionSnapshot:
    """Freeze the exact closed-bar permission visible when an arm was created."""
    try:
        decision_seconds = TF_SECONDS[decision_timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported decision timeframe: {decision_timeframe!r}") from exc
    decision_open = _utc_datetime(row.get("timestamp"))
    decision_bar = ImmutableBarRef(
        timeframe=decision_timeframe,
        open_time=decision_open,
        close_time=decision_open + timedelta(seconds=decision_seconds),
    )
    context: list[ImmutableBarRef] = []
    for timeframe in context_timeframes:
        try:
            seconds = TF_SECONDS[timeframe]
        except KeyError as exc:
            raise ValueError(f"unsupported context timeframe: {timeframe!r}") from exc
        context_close = floor_time(decision_bar.close_time, timeframe)
        context.append(
            ImmutableBarRef(
                timeframe=timeframe,
                open_time=context_close - timedelta(seconds=seconds),
                close_time=context_close,
            )
        )
    return FrozenPermissionSnapshot(
        decision_bar=decision_bar,
        context_bars=tuple(context),
        allow_long=allow_long,
        allow_short=allow_short,
        regime_state=_row_text(row, ("mreg_state", "regime_state"), "not_applicable"),
        direction=_row_text(
            row,
            ("mreg_h4", "bos15_htf", "sc15_htf_direction", "htf_direction"),
            "not_applicable",
        ),
        reason=_row_text(row, ("mreg_reason",), reason),
    )


__all__ = [
    "FrozenPermissionSnapshot",
    "ImmutableBarRef",
    "freeze_permission_from_row",
]
