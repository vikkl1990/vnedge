"""Versioned, causal market-stage research. No scanner or order authority."""

from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vnedge.dashboard.analyst_store import digest
from vnedge.dashboard.crypto_analyst import EXCHANGES, _true, read_window
from vnedge.data.bar_identity import bar_content_sha256
from vnedge.data.candles import TF_SECONDS, _decision_hash_row

SPEC = {
    "version": "market_stage_analyst_v1",
    "timeframes": ["4h", "1d"],
    "maximum_bars": 512,
    "minimum_bars": 60,
    "ema_span": 50,
    "slope_bars": 5,
    "slope_atr_threshold": 0.15,
    "trend_displacement_atr": 2.0,
    "range_bars": 20,
    "flat_displacement_atr": 1.0,
    "flat_width_atr": 6.0,
    "confirmation_closes": 2,
    "pivot_left": 3,
    "pivot_right": 3,
    "seed": "first valid close in bounded contiguous suffix; history is left-censored",
    "authority": "descriptive_only",
    "freshness_tf_multiple": 1.5,
}
SPEC_HASH = digest(SPEC)


def empty_stage(tf: str, reason: str) -> dict[str, Any]:
    return {
        "version": SPEC["version"],
        "spec_hash": SPEC_HASH,
        "timeframe": tf,
        "state": "unavailable",
        "stage": "unknown",
        "previous_stage": None,
        "stage_id": None,
        "as_of": None,
        "since": None,
        "bars_in_state": 0,
        "supports": [],
        "conflicts": [],
        "issues": [reason],
        "transitions": [],
        "watch": [],
        "metrics": {},
        "can_trade": False,
        "can_promote": False,
    }


def stage_rows(
    root: Path, exchange: str, symbol: str, tf: str, now: datetime
) -> list[dict[str, Any]]:
    if tf == "4h":
        return read_window(root, exchange, symbol, tf, now)
    if tf != "1d" or exchange not in EXCHANGES or not re.fullmatch(r"[A-Z0-9]{3,30}", symbol):
        raise ValueError("unsupported_stage_scope")
    import pyarrow.parquet as pq

    directory = root / f"exchange={exchange}" / symbol / tf
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("unsafe_stage_path")
    files = sorted(
        p
        for p in directory.glob("*.parquet")
        if re.fullmatch(r"\d{4}-\d{2}\.parquet", p.name) and p.name <= now.strftime("%Y-%m.parquet")
    )[-18:]
    rows = []
    for path in files:
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            raise ValueError("stage_partition_bound")
        parquet = pq.ParquetFile(path)
        if parquet.metadata.num_rows > 72:
            raise ValueError("stage_partition_rows")
        rows.extend(parquet.read().to_pandas().to_dict("records"))
    return rows


def analyse_stage(
    rows: list[dict[str, Any]], exchange: str, symbol: str, tf: str, now: datetime
) -> dict[str, Any]:
    if tf not in SPEC["timeframes"] or exchange not in EXCHANGES or now.tzinfo is None:
        raise ValueError("unsupported_stage_scope")
    seconds = TF_SECONDS[tf]
    ordered: dict[int, tuple[dict[str, Any], str | None]] = {}
    try:
        for row in rows:
            opened, closed = pd.Timestamp(row["open_time"]), pd.Timestamp(row["close_time"])
            if opened.tzinfo is None or closed.tzinfo is None:
                raise ValueError("naive_stage_clock")
            if closed > now:
                continue
            if not isinstance(row.get("is_closed"), (bool, np.bool_)):
                return empty_stage(tf, "closed_bar_proof_missing")
            if not row["is_closed"]:
                continue
            epoch = int(opened.timestamp())
            reason = None
            if (
                opened.microsecond
                or opened.nanosecond
                or epoch % seconds
                or (closed - opened).total_seconds() != seconds
            ):
                reason = "bar_clock_invalid"
            elif row.get("source") != "canonical_tick_lake":
                reason = "noncanonical_stage_source"
            elif row.get("data_quality") != "ok" or not _true(row.get("coverage_ok")):
                reason = "stage_coverage_unverified"
            elif any(
                row.get(k) is not None and row[k] != v
                for k, v in (("exchange", exchange), ("symbol", symbol), ("timeframe", tf))
            ):
                reason = "stage_identity_mismatch"
            else:
                expected = bar_content_sha256(
                    _decision_hash_row(row),
                    open_time=opened.to_pydatetime(),
                    close_time=closed.to_pydatetime(),
                    source="canonical_tick_lake",
                )
                if expected != row.get("content_sha256"):
                    reason = "stage_hash_invalid"
            o, h, lo, c, vol = (float(row[k]) for k in ("open", "high", "low", "close", "volume"))
            if (
                not all(math.isfinite(v) for v in (o, h, lo, c, vol))
                or vol < 0
                or not 0 < lo <= min(o, c) <= max(o, c) <= h
            ):
                reason = "stage_ohlcv_invalid"
            if epoch in ordered and ordered[epoch][0] != row:
                return empty_stage(tf, "conflicting_stage_duplicate")
            ordered[epoch] = (row, reason)
    except (KeyError, ValueError, TypeError, OverflowError):
        return empty_stage(tf, "malformed_stage_bar")
    items = sorted(ordered.items())[-SPEC["maximum_bars"] :]
    if not items:
        return empty_stage(tf, "no_closed_stage_bars")
    if items[-1][1][1]:
        return empty_stage(tf, items[-1][1][1])
    suffix, boundary = [], None
    expected_epoch = items[-1][0]
    for epoch, (row, problem) in reversed(items):
        if epoch != expected_epoch or problem:
            boundary = problem or "stage_history_gap"
            break
        suffix.append(row)
        expected_epoch -= seconds
    suffix.reverse()
    if len(suffix) < SPEC["minimum_bars"]:
        result = empty_stage(tf, "need_60_contiguous_stage_bars")
        result["issues"] += [boundary] if boundary else []
        return result
    df = pd.DataFrame(suffix)
    close, high, low = (pd.to_numeric(df[k]).astype(float) for k in ("close", "high", "low"))
    ema = close.ewm(span=50, adjust=False, min_periods=50).mean()
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(14).mean()
    stage, previous, trend_memory, candidate, streak = "unknown", None, None, None, 0
    since, transitions, pivots_h, pivots_l = 59, [], [], []
    metrics: dict[str, Any] = {}
    for i in range(len(df)):
        # A pivot becomes usable ONLY at the close of its third right child.
        p = i - 3
        if p >= 3:
            if high.iloc[p] > max(high.iloc[p - 3 : p].max(), high.iloc[p + 1 : p + 4].max()):
                pivots_h.append(float(high.iloc[p]))
            if low.iloc[p] < min(low.iloc[p - 3 : p].min(), low.iloc[p + 1 : p + 4].min()):
                pivots_l.append(float(low.iloc[p]))
        if i < 59:
            continue
        a = float(atr.iloc[i])
        if a <= 0:
            proposed = "unknown"
            slope = displacement = width = 0.0
        else:
            slope = float(ema.iloc[i] - ema.iloc[i - 5]) / a
            displacement = float(close.iloc[i] - close.iloc[i - 20]) / a
            width = float(high.iloc[i - 20 : i].max() - low.iloc[i - 20 : i].min()) / a
            if close.iloc[i] > ema.iloc[i] and slope > 0.15 and displacement >= 2:
                proposed = "advancing_trend"
            elif close.iloc[i] < ema.iloc[i] and slope < -0.15 and displacement <= -2:
                proposed = "declining_trend"
            elif abs(slope) <= 0.15 and abs(displacement) <= 1 and width <= 6:
                proposed = (
                    "base_after_decline"
                    if trend_memory == "down"
                    else "range_after_advance"
                    if trend_memory == "up"
                    else "unknown"
                )
            else:
                proposed = "transition"
        streak = streak + 1 if proposed == candidate else 1
        candidate = proposed
        if streak >= SPEC["confirmation_closes"] and proposed != stage:
            previous, stage, since = stage, proposed, i
            transitions.append(
                {
                    "from": previous,
                    "to": stage,
                    "at": pd.Timestamp(df.iloc[i]["close_time"]).isoformat(),
                    "bar_hash": suffix[i]["content_sha256"],
                }
            )
        if stage in ("advancing_trend", "declining_trend"):
            trend_memory = "up" if stage == "advancing_trend" else "down"
        metrics = {
            "ema50": float(ema.iloc[i]),
            "slope_5_atr": slope,
            "displacement_20_atr": displacement,
            "range_width_atr": width,
            "atr14": a,
            "prior_range_high": float(high.iloc[i - 20 : i].max()),
            "prior_range_low": float(low.iloc[i - 20 : i].min()),
            "confirmed_higher_lows": len(pivots_l) >= 2 and pivots_l[-1] > pivots_l[-2],
            "confirmed_lower_highs": len(pivots_h) >= 2 and pivots_h[-1] < pivots_h[-2],
            "relative_strength": None,
        }
    asof = pd.Timestamp(suffix[-1]["close_time"]).to_pydatetime()
    upper, lower = metrics["prior_range_high"], metrics["prior_range_low"]
    watches = [
        {
            "toward": "advancing_trend",
            "condition": f"Closed {tf} close > {upper:.8g}, EMA50 slope/ATR > 0.15 and 20-bar displacement/ATR >= 2 for two closes.",
            "level": upper,
        },
        {
            "toward": "declining_trend",
            "condition": f"Closed {tf} close < {lower:.8g}, EMA50 slope/ATR < -0.15 and 20-bar displacement/ATR <= -2 for two closes.",
            "level": lower,
        },
    ]
    conflicts = ["relative_strength_not_bound", "participant_identity_not_observable"]
    if candidate != stage:
        conflicts.append("stage_change_pending_second_close")
    body = {
        "version": SPEC["version"],
        "spec_hash": SPEC_HASH,
        "timeframe": tf,
        "stage": stage,
        "previous_stage": previous,
        "candidate": candidate,
        "state": "current" if (now - asof).total_seconds() <= seconds * 1.5 else "stale",
        "as_of": asof.isoformat(),
        "since": pd.Timestamp(suffix[since]["close_time"]).isoformat(),
        "bars_in_state": len(suffix) - since,
        "duration_seconds": (len(suffix) - 1 - since) * seconds,
        "memory_origin": pd.Timestamp(suffix[0]["open_time"]).isoformat(),
        "memory_left_censored": True,
        "prior_direction": trend_memory,
        "supports": [
            f"EMA50 five-bar slope: {metrics['slope_5_atr']:.3f} ATR",
            f"20-bar displacement: {metrics['displacement_20_atr']:.3f} ATR",
            f"Prior range width: {metrics['range_width_atr']:.3f} ATR",
        ],
        "conflicts": conflicts,
        "issues": [boundary] if boundary else [],
        "metrics": metrics,
        "transitions": transitions[-24:],
        "watch": watches,
        "invalidation": f"Base/advance thesis invalidated by a closed {tf} close below {lower:.8g}; decline/range-top thesis challenged above {upper:.8g}. These reference levels are frozen for this report, not orders.",
        "can_trade": False,
        "can_promote": False,
        "note": "Accumulation/distribution are hypotheses. Bounded causal history, not a compulsory cycle or validated edge.",
    }
    body["stage_id"] = digest(
        [SPEC_HASH, exchange, symbol, tf, [r["content_sha256"] for r in suffix]]
    )
    return body
