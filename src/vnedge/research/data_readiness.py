"""Read-only experiment input diagnostics; never select a smaller test window.

The admission verdict remains experiment_packet.preflight. This report explains
the supplied frame, without filling, repairing, sorting or filtering that frame.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from vnedge.data.candles import TF_SECONDS
from vnedge.strategy.arm_evidence import assert_decision_row

MAX_GAP_RANGES = 32


def describe_research_input(
    candles: pd.DataFrame, *, exchange: str, symbol: str, timeframe: str,
    required_bars: int, exact_volume: bool, now: datetime,
) -> dict[str, Any]:
    seconds = TF_SECONDS[timeframe]
    step = timedelta(seconds=seconds)
    times: list[datetime] = []
    valid: list[datetime] = []
    failures: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    out_of_order = 0
    previous: datetime | None = None
    for row in candles.to_dict("records"):
        source = row.get("candle_source")
        sources[str(source) if isinstance(source, str) and source else "unreported"] += 1
        try:
            stamp = pd.Timestamp(row.get("timestamp"))
            if pd.isna(stamp) or stamp.tzinfo is None or stamp.microsecond or stamp.nanosecond:
                raise ValueError("invalid_timestamp")
            if int(stamp.timestamp()) % seconds:
                raise ValueError("unaligned_timestamp")
            opened = stamp.to_pydatetime().astimezone(UTC)
            if previous is not None and opened < previous:
                out_of_order += 1
            previous = opened
            times.append(opened)
        except (ValueError, TypeError, OverflowError):
            failures["timestamp_invalid"] += 1
            continue
        try:
            if not isinstance(row.get("is_closed"), (bool, np.bool_)) or not row["is_closed"]:
                raise ValueError("decision_row_not_closed")
            ref = assert_decision_row(row, timeframe=timeframe)
            if ref.close_time > now:
                raise ValueError("future_bar")
            for key, expected in (("exchange", exchange), ("symbol", symbol), ("timeframe", timeframe)):
                if row.get(key) != expected:
                    raise ValueError(f"{key}_identity_missing_or_mismatch")
            if not isinstance(row.get("coverage_ok"), (bool, np.bool_)) or not row["coverage_ok"]:
                raise ValueError("coverage_unproven")
            volume = float(row.get("volume", float("nan")))
            if not np.isfinite(volume) or volume < 0:
                raise ValueError("base_volume_invalid")
            if exact_volume:
                quote = float(row.get("quote_volume", float("nan")))
                if volume <= 0 or not np.isfinite(quote) or quote <= 0:
                    raise ValueError("exact_volume_window_not_ready")
            valid.append(opened)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            failures[str(exc)] += 1

    counts = Counter(times)
    duplicate_slots = sum(n > 1 for n in counts.values())
    observed = sorted(counts)
    # A duplicate slot is ambiguous even if both rows independently hash.
    verified = sorted(t for t in valid if counts[t] == 1)
    ranges: list[dict[str, Any]] = []
    missing = 0
    range_count = 0
    for left, right in zip(observed, observed[1:]):
        slots = int((right - left).total_seconds() / seconds) - 1
        if slots > 0:
            missing += slots
            range_count += 1
            ranges.append({"from_open": (left + step).isoformat(),
                           "to_open": (right - step).isoformat(), "missing_bars": slots})
            ranges = ranges[-MAX_GAP_RANGES:]
    runs: list[list[datetime]] = []
    for opened in verified:
        if not runs or opened != runs[-1][-1] + step:
            runs.append([])
        runs[-1].append(opened)
    longest = max(runs, key=len, default=[])
    tail = runs[-1] if runs and observed and runs[-1][-1] == observed[-1] else []
    blockers = []
    if len(candles) < required_bars:
        blockers.append("history_shortfall")
    if failures:
        blockers.append("row_proof_failures")
    if missing:
        blockers.append("internal_gaps")
    if duplicate_slots or out_of_order:
        blockers.append("ambiguous_row_order")
    return {
        "schema_version": 1, "scope": "supplied_experiment_frame", "observed_at": now.isoformat(),
        "exchange": exchange, "symbol": symbol, "timeframe": timeframe,
        "stored_rows": len(candles), "verified_unique_bars": len(verified),
        "required_bars": required_bars, "row_shortfall": max(0, required_bars - len(candles)),
        "contiguous_shortfall": max(0, required_bars - len(longest)),
        "first_open": observed[0].isoformat() if observed else None,
        "last_open": observed[-1].isoformat() if observed else None,
        "longest_contiguous_bars": len(longest), "latest_contiguous_bars": len(tail),
        "longest_from_open": longest[0].isoformat() if longest else None,
        "longest_to_open": longest[-1].isoformat() if longest else None,
        "missing_internal_bars": missing, "gap_range_count": range_count,
        "gap_ranges": ranges, "gap_ranges_truncated": range_count > len(ranges),
        "duplicate_slots": duplicate_slots, "out_of_order_rows": out_of_order,
        "invalid_row_counts": dict(sorted(failures.items())), "source_counts": dict(sorted(sources.items())),
        "blockers": blockers, "historical_coverage": "outside_frame_unknown",
        "repair_authorized": False, "can_trade": False, "can_promote": False,
    }
