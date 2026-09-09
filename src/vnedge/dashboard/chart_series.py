"""Chart series for the operator cockpit: canonical candles plus trade markers.

Two things the pulse endpoints do not provide and forensics needs:

* raw OHLCV from the CANONICAL store, so what is charted is the same series
  research and shadow are supposed to read -- not a fourth source invented for
  the UI;
Markers are deliberately NOT served here. MarketPulse already builds them from
journal scanner_events and calls setMarkers itself; a second server-side path
would be two things doing one job, which is the exact defect the 2026-08-22
audit catalogued.

Read-only. This module has no order, promotion or settings authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import math
from typing import Any

import numpy as np
import pandas as pd

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.data.candles import CandleParquetStore, TF_SECONDS, _decision_hash_row
from vnedge.data.parquet_store import sanitize_symbol
from vnedge.data.symbols import canonical_symbol

#: Never hand an unbounded series to a browser; a year of 1m bars is 525k rows.
MAX_BARS = 5_000


class ChartReadError(ValueError):
    """A failed read must not masquerade as the beginning of history."""


def _chart_rows(store: Any, symbol: str, timeframe: str) -> list[dict[str, Any]]:
    if timeframe not in TF_SECONDS:
        raise ChartReadError("unsupported_chart_timeframe")
    if isinstance(store, CandleParquetStore):
        # read_records intentionally supports legacy reconstruction. A chart
        # must instead show only the proof that was actually persisted.
        root = store.root / f"exchange={store.exchange}" if store.exchange else store.root
        directory = root / sanitize_symbol(canonical_symbol(symbol)) / timeframe
        frames = [pd.read_parquet(p) for p in sorted(directory.glob("*.parquet"))]
        return pd.concat(frames, ignore_index=True).to_dict("records") if frames else []
    # Non-persisted adapters (including demo fixtures) are never attested.
    return [vars(c) for c in store.read(symbol, timeframe)]


def _text(value: Any) -> str | None:
    return None if value is None or pd.isna(value) else str(value)


def _bool(value: Any) -> bool | None:
    return bool(value) if isinstance(value, (bool, np.bool_)) else None


def candles_payload(
    store: Any,
    symbol: str,
    timeframe: str,
    *,
    limit: int = 500,
    from_ms: int | None = None,
    to_ms: int | None = None,
    revision_before_ms: int | None = None,
) -> dict[str, Any]:
    """Canonical OHLCV shaped for lightweight-charts.

    Decimals become floats only at this boundary. They stay Decimal everywhere
    the money math happens; JSON has no decimal type and the chart needs none.
    """
    limit = max(1, min(int(limit), MAX_BARS))
    now = datetime.now(UTC)
    try:
        rows = _chart_rows(store, symbol, timeframe)
        by_time: dict[int, dict[str, Any]] = {}
        for row in rows:
            opened = pd.Timestamp(row["open_time"])
            if opened.tzinfo is None or opened.microsecond or opened.nanosecond:
                raise ChartReadError("chart_open_time_invalid")
            epoch = int(opened.timestamp())
            if epoch % TF_SECONDS[timeframe]:
                raise ChartReadError("chart_open_time_unaligned")
            values = {k: float(row[k]) for k in ("open", "high", "low", "close", "volume")}
            if not all(math.isfinite(v) for v in values.values()):
                raise ChartReadError("chart_ohlcv_nonfinite")
            source = _text(row.get("source"))
            stored_hash = _text(row.get("content_sha256"))
            closed = _bool(row.get("is_closed"))
            coverage = _bool(row.get("coverage_ok"))
            quality = _text(row.get("data_quality"))
            close_time = pd.Timestamp(row["close_time"]) if _text(row.get("close_time")) else None
            hash_valid = False
            if stored_hash and source and close_time is not None:
                hash_valid = stored_hash == bar_content_sha256(
                    _decision_hash_row(row), open_time=opened.to_pydatetime(),
                    close_time=close_time.to_pydatetime(), source=source)
            identity_ok = all(_text(row.get(k)) in (None, expected) for k, expected in (
                ("exchange", getattr(store, "exchange", None)),
                ("symbol", canonical_symbol(symbol)), ("timeframe", timeframe)))
            geometry_ok = close_time is not None and close_time.tzinfo is not None and (
                close_time - opened).total_seconds() == TF_SECONDS[timeframe]
            verified = (source == "canonical_tick_lake" and hash_valid and identity_ok
                        and geometry_ok and quality == "ok" and coverage is True)
            state = ("CLOSED" if verified and closed is True and close_time <= now else
                     "WATCH" if verified and closed is False and opened <= now < close_time else
                     "UNVERIFIED")
            dto = {"time": epoch, **values, "source": source, "content_sha256": stored_hash,
                   "is_closed": closed, "data_quality": quality, "coverage_ok": coverage,
                   "close_time": int(close_time.timestamp()) if close_time is not None else None,
                   "hash_valid": hash_valid, "identity_ok": identity_ok,
                   "proof_state": state}
            if epoch in by_time and dto != by_time[epoch]:
                raise ChartReadError("chart_conflicting_duplicate")
            by_time[epoch] = dto
    except Exception as exc:
        raise ChartReadError(str(exc) if isinstance(exc, ChartReadError) else "chart_store_read_failed") from exc
    ordered = sorted(by_time.items())
    # Prefix fingerprints distinguish repairs/removals from ordinary appends.
    # They include excluded rows and proof changes, not just price floats.
    def digest(cutoff: int | None) -> str:
        return hashlib.sha256(json.dumps([dto for ts, dto in ordered if cutoff is not None and ts * 1000 <= cutoff],
                                        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    immutable_times = [ts * 1000 for ts, dto in ordered if dto["is_closed"] is True
                       and dto["close_time"] is not None and dto["close_time"] <= now.timestamp()]
    cutoff = max(immutable_times) if immutable_times else None
    series_revision = digest(cutoff)
    previous_revision = digest(revision_before_ms) if revision_before_ms is not None else None
    excluded: dict[str, int] = {}
    visible = []
    for ts, dto in ordered:
        if dto["source"] != "canonical_tick_lake" or not dto["identity_ok"]:
            label = dto["source"] or "unreported"
            excluded[label] = excluded.get(label, 0) + 1
        else:
            visible.append((ts, dto))
    ordered = visible
    if from_ms is not None:
        from_epoch = int(from_ms) // 1_000
        ordered = [(epoch, candle) for epoch, candle in ordered if epoch >= from_epoch]
    if to_ms is not None:
        to_epoch = int(to_ms) // 1_000
        ordered = [(epoch, candle) for epoch, candle in ordered if epoch <= to_epoch]
    tail = ordered[-limit:]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": "canonical_lake",  # storage/transport label, NOT row provenance
        "source_policy": "canonical_tick_lake_only",
        "exchange": getattr(store, "exchange", "unreported"),
        "status": "OK" if tail else "UNVERIFIED" if excluded else "EMPTY",
        "excluded_sources": excluded,
        "series_revision": series_revision,
        "revision_cutoff_ms": cutoff,
        "previous_revision": previous_revision,
        "fetched_at": now.isoformat(),
        "count": len(tail),
        "truncated": len(ordered) > len(tail),
        "range": {"from_ms": from_ms, "to_ms": to_ms},
        "candles": [c for _, c in tail],
    }


def mechanism_context_payload(store: Any, symbol: str, timeframe: str, *,
                              limit: int = 600) -> dict[str, Any]:
    """Drawable mechanism context for the chart, from the canonical store.

    Same store discipline as ``candles_payload``; the context itself comes
    from ``ml.mechanism_features.mechanism_context`` — the model's own
    definitions — so the chart can never show the operator a different
    market than the ML plane sees. Read-only, presentation-only.
    """
    import pandas as pd

    from vnedge.ml.mechanism_features import mechanism_context

    limit = max(1, min(int(limit), MAX_BARS))
    try:
        rows = list(store.read(symbol, timeframe))
    except Exception:  # noqa: BLE001 - read-only UI must degrade on store failures
        rows = []
    base = {"symbol": symbol, "timeframe": timeframe, "source": "canonical_lake"}
    if not rows:
        return {**base, "ready": False, "bars": 0}
    rows = rows[-limit:]
    frame = pd.DataFrame(
        {
            "timestamp": [candle.open_time for candle in rows],
            "open": [float(candle.open) for candle in rows],
            "high": [float(candle.high) for candle in rows],
            "low": [float(candle.low) for candle in rows],
            "close": [float(candle.close) for candle in rows],
            "volume": [float(candle.volume) for candle in rows],
        }
    )
    context = mechanism_context(frame)
    last = rows[-1]
    epoch = (
        int(last.open_time.replace(tzinfo=UTC).timestamp())
        if last.open_time.tzinfo is None
        else int(last.open_time.timestamp())
    )
    return {**base, "as_of": epoch, **context}
