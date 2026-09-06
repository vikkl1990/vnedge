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
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import pandas as pd

from vnedge.data.candles import TF_SECONDS, BarState, floor_time

TRUSTED_PERMISSION_CANDLE_SOURCES = frozenset(
    {
        "canonical_tick_lake",
        "router",
    }
)


class MissingHtfContext(ValueError):
    """A declared permission timeframe has no usable bound closed bar."""

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(str(item) for item in missing)
        super().__init__("htf_context_missing:" + ",".join(self.missing))


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


def _true_flag(value: object) -> bool:
    """Accept Python/pandas boolean truth without treating NaN as true."""
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        return False
    return bool(value)


@dataclass(frozen=True, slots=True)
class ImmutableBarRef:
    timeframe: str
    open_time: datetime
    close_time: datetime
    version: int = 1
    state: Literal["closed_immutable"] = "closed_immutable"
    source: str = "unreported"
    content_sha256: str | None = None

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
        if not self.source.strip():
            raise ValueError("evidence bar source must be non-empty")
        if self.content_sha256 is not None and (
            len(self.content_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.content_sha256.lower())
        ):
            raise ValueError("evidence bar content_sha256 must be a SHA-256 hex digest")
        object.__setattr__(self, "open_time", open_time)
        object.__setattr__(self, "close_time", close_time)

    def as_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "version": self.version,
            "state": self.state,
            "source": self.source,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ImmutableBarRef:
        return cls(
            timeframe=str(payload["timeframe"]),
            open_time=_utc_datetime(payload["open_time"]),
            close_time=_utc_datetime(payload["close_time"]),
            version=int(payload.get("version", 1)),
            state=str(payload.get("state", BarState.CLOSED_IMMUTABLE.value)),  # type: ignore[arg-type]
            source=str(payload.get("source", "unreported")),
            content_sha256=(
                str(payload["content_sha256"])
                if payload.get("content_sha256") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class FrozenPermissionSnapshot:
    decision_bar: ImmutableBarRef
    context_bars: tuple[ImmutableBarRef, ...]
    allow_long: bool
    allow_short: bool
    regime_state: str
    direction: str
    reason: str
    regime_version: str = "unreported"

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
            "regime_version": self.regime_version,
        }
        if include_id:
            payload["snapshot_id"] = self.snapshot_id
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FrozenPermissionSnapshot:
        decision = payload.get("decision_bar")
        context = payload.get("context_bars", ())
        if not isinstance(decision, Mapping):
            raise TypeError("permission snapshot decision_bar must be an object")
        if not isinstance(context, Sequence) or isinstance(context, (str, bytes)):
            raise TypeError("permission snapshot context_bars must be a sequence")
        result = cls(
            decision_bar=ImmutableBarRef.from_dict(decision),
            context_bars=tuple(
                ImmutableBarRef.from_dict(item)
                for item in context
                if isinstance(item, Mapping)
            ),
            allow_long=bool(payload.get("allow_long")),
            allow_short=bool(payload.get("allow_short")),
            regime_state=str(payload.get("regime_state", "unreported")),
            direction=str(payload.get("direction", "unreported")),
            reason=str(payload.get("reason", "unreported")),
            regime_version=str(payload.get("regime_version", "unreported")),
        )
        supplied_id = payload.get("snapshot_id")
        if supplied_id is not None and str(supplied_id) != result.snapshot_id:
            raise ValueError("permission snapshot_id does not match snapshot payload")
        if len(result.context_bars) != len(context):
            raise TypeError("permission snapshot context_bars contain a non-object")
        return result


def _row_text(row: Mapping[str, Any], names: Sequence[str], default: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() and str(value).lower() != "nan":
            return str(value)
    return default


def _evidence_source(row: Mapping[str, Any], *, require_closed_truth: bool) -> str:
    source = str(row.get("candle_source", "unreported")).strip()
    if require_closed_truth and source not in TRUSTED_PERMISSION_CANDLE_SOURCES:
        raise ValueError(f"untrusted permission candle source: {source}")
    return source


def _canonical_number(value: object) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    normalized = number.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def bar_content_sha256(
    row: Mapping[str, Any],
    *,
    open_time: datetime,
    close_time: datetime,
    source: str,
) -> str:
    """Hash normalized bar content, independent of Python/pandas scalar types."""
    identity = {
        "open_time": open_time.isoformat(),
        "close_time": close_time.isoformat(),
        "open": _canonical_number(row.get("open")),
        "high": _canonical_number(row.get("high")),
        "low": _canonical_number(row.get("low")),
        "close": _canonical_number(row.get("close")),
        "volume": _canonical_number(row.get("volume")),
        "quote_volume": _canonical_number(row.get("quote_volume")),
        "trade_count": _canonical_number(row.get("trade_count")),
        "source": source,
    }
    # Compatibility-only provenance for old synthetic fixtures whose raw
    # timestamp was not aligned to the declared TF. Production canonical rows
    # never set this field because their identity is already exact.
    if row.get("source_open_time") is not None:
        identity["source_open_time"] = str(row["source_open_time"])
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# Compatibility alias for older internal call sites.  New transport/parity
# code uses the public name so the ARM envelope and router evidence hash the
# same normalized candle contract.
_bar_content_sha256 = bar_content_sha256


def _last_eligible_context_row(
    frame: pd.DataFrame | None,
    *,
    timeframe: str,
    decision_close: datetime,
) -> dict[str, Any] | None:
    """Select the actual last context row closed by ``decision_close``.

    This is a store lookup, not calendar inference.  A future/forming-only
    frame therefore returns no eligible row even when flooring the decision
    timestamp would produce a plausible-looking bar identity.
    """

    try:
        seconds = TF_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported context timeframe: {timeframe!r}") from exc
    if frame is None or frame.empty:
        return None
    timestamp_column = "timestamp" if "timestamp" in frame.columns else "open_time"
    if timestamp_column not in frame.columns:
        return None
    opens = pd.to_datetime(frame[timestamp_column], utc=True, errors="coerce")
    if "close_time" in frame.columns:
        closes = pd.to_datetime(frame["close_time"], utc=True, errors="coerce")
    else:
        closes = opens + pd.Timedelta(seconds=seconds)
    decision = pd.Timestamp(_utc_datetime(decision_close))
    eligible = frame.loc[opens.notna() & closes.notna() & closes.le(decision)].copy()
    if eligible.empty:
        return None
    eligible["__evidence_open"] = opens.loc[eligible.index]
    row = eligible.sort_values("__evidence_open").iloc[-1].drop(labels="__evidence_open")
    return row.to_dict()


def last_eligible_context_bar(
    frame: pd.DataFrame | None,
    *,
    timeframe: str,
    decision_close: datetime,
) -> ImmutableBarRef | None:
    """Return the exact trusted immutable context bar visible to a decision."""

    row = _last_eligible_context_row(
        frame,
        timeframe=timeframe,
        decision_close=decision_close,
    )
    if row is None:
        return None
    seconds = TF_SECONDS[timeframe]
    declared_timeframe = str(row.get("timeframe") or timeframe)
    if declared_timeframe != timeframe:
        raise ValueError(
            f"bound context timeframe mismatch: expected {timeframe}, got {declared_timeframe}"
        )
    if not _true_flag(row.get("is_closed")):
        raise ValueError(f"bound context row is not closed: {timeframe}")
    if str(row.get("data_quality", "")).lower() != "ok":
        raise ValueError(f"bound context row data quality is not ok: {timeframe}")
    source = _evidence_source(row, require_closed_truth=True)
    open_time = _utc_datetime(row.get("timestamp", row.get("open_time")))
    close_time = _utc_datetime(
        row.get("close_time", open_time + timedelta(seconds=seconds))
    )
    return ImmutableBarRef(
        timeframe=timeframe,
        open_time=open_time,
        close_time=close_time,
        source=source,
        content_sha256=bar_content_sha256(
            row,
            open_time=open_time,
            close_time=close_time,
            source=source,
        ),
    )


def missing_bound_context(
    *,
    context_frames: Mapping[str, pd.DataFrame | None],
    context_health: Mapping[str, bool],
    required_context: Sequence[str],
    decision_close: datetime,
) -> tuple[str, ...]:
    """Explain which declared HTF permissions cannot be sourced."""

    missing: list[str] = []
    for timeframe in required_context:
        if not context_health.get(timeframe, False):
            missing.append(f"{timeframe}:unhealthy")
            continue
        try:
            ref = last_eligible_context_bar(
                context_frames.get(timeframe),
                timeframe=timeframe,
                decision_close=decision_close,
            )
        except (TypeError, ValueError):
            missing.append(f"{timeframe}:invalid")
            continue
        if ref is None:
            missing.append(f"{timeframe}:absent")
    return tuple(missing)


def freeze_permission_from_bound_frames(
    row: Mapping[str, Any],
    *,
    decision_timeframe: str,
    context_frames: Mapping[str, pd.DataFrame | None],
    context_health: Mapping[str, bool],
    required_context: Sequence[str],
    allow_long: bool,
    allow_short: bool,
    reason: str,
    regime_version: str,
) -> FrozenPermissionSnapshot:
    """Freeze permission from actual bound HTF rows or reject before an arm.

    Production context-aware strategies use this entry point.  The older
    row-level helper remains useful for context-free and compatibility tests,
    but it is not permitted to infer declared HTF identities here.
    """

    decision_open = _utc_datetime(row.get("timestamp", row.get("open_time")))
    try:
        decision_seconds = TF_SECONDS[decision_timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported decision timeframe: {decision_timeframe!r}") from exc
    decision_close = decision_open + timedelta(seconds=decision_seconds)
    missing = missing_bound_context(
        context_frames=context_frames,
        context_health=context_health,
        required_context=required_context,
        decision_close=decision_close,
    )
    if missing:
        raise MissingHtfContext(missing)
    context_rows: dict[str, Mapping[str, Any]] = {}
    for timeframe in required_context:
        bound = _last_eligible_context_row(
            context_frames.get(timeframe),
            timeframe=timeframe,
            decision_close=decision_close,
        )
        if bound is None:  # defensive: selection was proven immediately above
            raise MissingHtfContext((f"{timeframe}:absent",))
        context_rows[timeframe] = bound
    return freeze_permission_from_row(
        row,
        decision_timeframe=decision_timeframe,
        context_timeframes=required_context,
        allow_long=allow_long,
        allow_short=allow_short,
        reason=reason,
        context_rows=context_rows,
        regime_version=regime_version,
        require_bound_context=True,
        require_closed_truth=True,
    )


def freeze_permission_from_row(
    row: Mapping[str, Any],
    *,
    decision_timeframe: str,
    context_timeframes: Sequence[str],
    allow_long: bool,
    allow_short: bool,
    reason: str,
    context_rows: Mapping[str, Mapping[str, Any]] | None = None,
    regime_version: str = "unreported",
    require_bound_context: bool = False,
    require_closed_truth: bool = False,
) -> FrozenPermissionSnapshot:
    """Freeze the exact closed-bar permission visible when an arm was created.

    ``context_rows`` is the authoritative form: callers pass the actual last
    eligible row selected from each bound HTF frame.  Calendar inference is
    retained only for context-free/legacy evidence and must not be used by a
    strategy that declares ``require_bound_context``.
    """
    try:
        decision_seconds = TF_SECONDS[decision_timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported decision timeframe: {decision_timeframe!r}") from exc
    decision_open = _utc_datetime(row.get("timestamp", row.get("open_time")))
    if require_closed_truth:
        if not regime_version.strip() or regime_version == "unreported":
            raise ValueError("strict permission evidence requires a regime version")
        if not _true_flag(row.get("is_closed")):
            raise ValueError("decision evidence row is not closed")
        if str(row.get("data_quality", "")).lower() != "ok":
            raise ValueError("decision evidence row data quality is not ok")
    decision_source = _evidence_source(row, require_closed_truth=require_closed_truth)
    decision_close = decision_open + timedelta(seconds=decision_seconds)
    decision_bar = ImmutableBarRef(
        timeframe=decision_timeframe,
        open_time=decision_open,
        close_time=decision_close,
        source=decision_source,
        content_sha256=bar_content_sha256(
            row,
            open_time=decision_open,
            close_time=decision_close,
            source=decision_source,
        ),
    )
    context: list[ImmutableBarRef] = []
    for timeframe in context_timeframes:
        try:
            seconds = TF_SECONDS[timeframe]
        except KeyError as exc:
            raise ValueError(f"unsupported context timeframe: {timeframe!r}") from exc
        bound = context_rows.get(timeframe) if context_rows is not None else None
        if bound is None and require_bound_context:
            raise ValueError(f"required bound context row is missing: {timeframe}")
        if bound is None:
            context_close = floor_time(decision_bar.close_time, timeframe)
            context_open = context_close - timedelta(seconds=seconds)
            source = "calendar_inferred"
            content_sha256 = None
        else:
            context_open = _utc_datetime(bound.get("timestamp", bound.get("open_time")))
            context_close = _utc_datetime(
                bound.get("close_time", context_open + timedelta(seconds=seconds))
            )
            if require_closed_truth:
                if not _true_flag(bound.get("is_closed")):
                    raise ValueError(f"bound context row is not closed: {timeframe}")
                if str(bound.get("data_quality", "")).lower() != "ok":
                    raise ValueError(f"bound context row data quality is not ok: {timeframe}")
            source = _evidence_source(bound, require_closed_truth=require_closed_truth)
            content_sha256 = bar_content_sha256(
                bound,
                open_time=context_open,
                close_time=context_close,
                source=source,
            )
        context.append(
            ImmutableBarRef(
                timeframe=timeframe,
                open_time=context_open,
                close_time=context_close,
                source=source,
                content_sha256=content_sha256,
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
        regime_version=regime_version,
    )


__all__ = [
    "TRUSTED_PERMISSION_CANDLE_SOURCES",
    "FrozenPermissionSnapshot",
    "ImmutableBarRef",
    "MissingHtfContext",
    "bar_content_sha256",
    "freeze_permission_from_bound_frames",
    "freeze_permission_from_row",
    "last_eligible_context_bar",
    "missing_bound_context",
]
