"""Rolling latency measurement for the tick-fast, bar-slow runtime.

The clocks are deliberately separate:

* ``ingest_lag_ms`` — exchange event timestamp to local receipt. This measures
  how quickly VNEDGE sees a tick; it never authorizes a strategy decision.
* ``bar_close_processing_ms`` — canonical bucket close to the point the closed
  bar reaches the decision loop. ``feed_lag_ms`` is retained as a compatibility
  alias for older snapshots and dashboards.
* ``decision_lag_ms`` — monotonic compute time from closed bar in hand through
  strategy preparation and signal evaluation.
* ``clock_skew_ms`` — magnitude by which an exchange timestamp is ahead of the
  receiving UTC clock. A future event is a data-quality observation, not a
  negative/fast latency sample.

Only the two bar-path measurements compose into candle-to-decision latency.
Tick ingest freshness is observability and stop-management input; it cannot
make a closed-bar strategy evaluate early.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from math import isfinite

INGEST_LAG_MS = "ingest_lag_ms"
BAR_CLOSE_PROCESSING_MS = "bar_close_processing_ms"
FEED_LAG_MS = "feed_lag_ms"  # legacy alias for BAR_CLOSE_PROCESSING_MS
DECISION_LAG_MS = "decision_lag_ms"
CLOCK_SKEW_MS = "clock_skew_ms"

_TF_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_to_seconds(timeframe: str) -> int:
    """``'1m'`` -> 60, ``'5m'`` -> 300, ``'1h'`` -> 3600, ``'4h'`` -> 14400.

    Raises ``ValueError`` on an unparseable timeframe rather than guessing —
    a wrong bar length would silently poison every feed-lag number, which is
    worse than a loud failure the caller can choose to swallow.
    """
    tf = (timeframe or "").strip().lower()
    if len(tf) < 2 or tf[-1] not in _TF_UNIT_SECONDS or not tf[:-1].isdigit():
        raise ValueError(f"unparseable timeframe: {timeframe!r}")
    return int(tf[:-1]) * _TF_UNIT_SECONDS[tf[-1]]


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile on a pre-sorted list; q in [0, 1]."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = round(q * (len(sorted_vals) - 1))
    return sorted_vals[idx]


class LatencyTracker:
    """Bounded rolling windows of latency samples, keyed by metric name."""

    def __init__(self, maxlen: int = 240) -> None:
        self.maxlen = maxlen
        self._series: dict[str, deque[float]] = {}

    def record(self, name: str, ms: float) -> None:
        """Append one sample (milliseconds).

        A non-finite sample is dropped: it would poison p95/max and, if it ever
        reached the dashboard snapshot, break JSON serialization (``/state``
        uses ``allow_nan=False``) and freeze the whole board.
        """
        if not isfinite(ms):
            return
        series = self._series.get(name)
        if series is None:
            series = self._series[name] = deque(maxlen=self.maxlen)
        series.append(float(ms))

    @staticmethod
    def _utc(value: datetime, *, label: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")
        return value.astimezone(UTC)

    def record_event(self, event_time: datetime, received_at: datetime) -> dict[str, float]:
        """Record exchange-event to local-receipt latency.

        When ``event_time`` is ahead of ``received_at``, ingest lag is clamped
        to zero and the future-clock magnitude is recorded separately. This
        prevents clock skew from masquerading as excellent network latency.
        The returned values are useful to a caller that also wants to emit an
        operator alert without redoing timestamp arithmetic.
        """
        event = self._utc(event_time, label="event_time")
        received = self._utc(received_at, label="received_at")
        delta_ms = (received - event).total_seconds() * 1000.0
        ingest_ms = max(0.0, delta_ms)
        skew_ms = max(0.0, -delta_ms)
        self.record(INGEST_LAG_MS, ingest_ms)
        self.record(CLOCK_SKEW_MS, skew_ms)
        return {INGEST_LAG_MS: ingest_ms, CLOCK_SKEW_MS: skew_ms}

    def record_bar_close(self, close_time: datetime, observed_at: datetime) -> dict[str, float]:
        """Record closed-bucket arrival at the decision loop.

        ``close_time`` is the canonical boundary, not the last trade timestamp.
        Future closes are clamped out of latency and exposed through
        ``clock_skew_ms``. The legacy ``feed_lag_ms`` series receives the exact
        same sample during the migration window.
        """
        close = self._utc(close_time, label="close_time")
        observed = self._utc(observed_at, label="observed_at")
        delta_ms = (observed - close).total_seconds() * 1000.0
        lag_ms = max(0.0, delta_ms)
        skew_ms = max(0.0, -delta_ms)
        self.record(BAR_CLOSE_PROCESSING_MS, lag_ms)
        self.record(FEED_LAG_MS, lag_ms)
        if skew_ms > 0.0:
            self.record(CLOCK_SKEW_MS, skew_ms)
        return {BAR_CLOSE_PROCESSING_MS: lag_ms, CLOCK_SKEW_MS: skew_ms}

    def stats(self, name: str) -> dict | None:
        """`{last, p50, p95, max, n}` for one metric, or None if no samples."""
        series = self._series.get(name)
        if not series:
            return None
        vals = sorted(series)
        return {
            "last": round(series[-1], 2),
            "p50": round(_percentile(vals, 0.50), 2),
            "p95": round(_percentile(vals, 0.95), 2),
            "max": round(vals[-1], 2),
            "n": len(vals),
        }

    def snapshot(self) -> dict:
        """All metrics -> their stats. Empty dict when nothing recorded yet."""
        out = {}
        for name in self._series:
            st = self.stats(name)
            if st is not None:
                out[name] = st
        return out
