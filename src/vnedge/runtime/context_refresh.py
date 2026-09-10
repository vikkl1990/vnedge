"""Exact, source-preserving HTF rows. Never a decision-candle fallback."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.data.candles import TF_SECONDS
from vnedge.data.symbols import canonical_symbol

OFFICIAL_CONTEXT_SOURCE = "exchange_ohlcv_validated"


def exact_context_row(
    frame: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    opened: datetime,
    allowed_sources: tuple[str, ...],
) -> dict[str, Any] | None:
    """Validate one exact closed identity; no floor/asof substitution or repair."""
    if frame.empty or "timestamp" not in frame:
        return None
    matches = frame.loc[pd.to_datetime(frame.timestamp, utc=True, errors="coerce").eq(opened)]
    if len(matches) != 1:
        return None
    row = matches.iloc[0].to_dict()
    try:
        seconds = TF_SECONDS[timeframe]
        if opened.tzinfo is None or opened.timestamp() % seconds:
            return None
        if canonical_symbol(str(row.get("symbol", ""))) != canonical_symbol(symbol):
            return None
        if row.get("timeframe") != timeframe or row.get("candle_source") not in allowed_sources:
            return None
        if row.get("is_closed") != True or row.get("coverage_ok") != True:
            return None
        if row.get("data_quality") != "ok":
            return None
        o, h, low, c = (float(row[k]) for k in ("open", "high", "low", "close"))
        if not all(math.isfinite(x) and x > 0 for x in (o, h, low, c)):
            return None
        if not low <= min(o, c) <= max(o, c) <= h:
            return None
        volume = float(row["volume"])
        if not math.isfinite(volume) or volume < 0:
            return None
        expected = bar_content_sha256(
            row,
            open_time=opened,
            close_time=opened + timedelta(seconds=seconds),
            source=row["candle_source"],
        )
        if row.get("content_sha256") != expected:
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return row


def official_context_row(
    rows: list[list],
    *,
    symbol: str,
    timeframe: str,
    opened: datetime,
    now: datetime,
) -> dict[str, Any] | None:
    """Validate a closed official OHLC response, without inventing tape volume."""
    seconds = TF_SECONDS[timeframe]
    if opened.tzinfo is None or now.tzinfo is None or opened + timedelta(seconds=seconds) > now:
        return None
    target = int(opened.timestamp() * 1000)
    matches = [r for r in rows if len(r) >= 6 and r[0] == target]
    if len(matches) != 1:
        return None
    values = matches[0]
    row: dict[str, Any] = dict(zip(("open", "high", "low", "close", "volume"), values[1:6]))
    row.update(
        timestamp=opened,
        symbol=canonical_symbol(symbol),
        timeframe=timeframe,
        candle_source=OFFICIAL_CONTEXT_SOURCE,
        is_closed=True,
        data_quality="ok",
        coverage_ok=True,
    )
    # coverage_ok here is official OHLC validation, NOT trade-tape completeness.
    # Missing quote volume/trade count remain absent, never fabricated for VWAP.
    try:
        row["content_sha256"] = bar_content_sha256(
            row,
            open_time=opened,
            close_time=opened + timedelta(seconds=seconds),
            source=OFFICIAL_CONTEXT_SOURCE,
        )
    except (TypeError, ValueError, KeyError):
        return None
    return exact_context_row(
        pd.DataFrame([row]),
        symbol=symbol,
        timeframe=timeframe,
        opened=opened,
        allowed_sources=(OFFICIAL_CONTEXT_SOURCE,),
    )
