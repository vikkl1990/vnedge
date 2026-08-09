"""Rolling latency measurement for the live loop.

Two numbers matter for a closed-candle system, and they compose:

* ``feed_lag_ms``     — how stale a candle is by the time we act on it
                        (``now - candle_close``). Network transit, exchange
                        emit delay, AND any single-loop saturation queue-wait
                        are all captured honestly, because ``now`` is the wall
                        clock at the moment we dequeue the closed bar.
* ``decision_lag_ms`` — compute time from candle-in-hand to signal
                        (``strategy.prepare`` + ``strategy.signal``).

Their sum is the true candle -> signal latency. This tracker keeps a bounded
rolling window per metric and reports last / p50 / p95 / max, so the dashboard
and any stall detector read the exact same honest numbers rather than each
re-deriving them.
"""
from __future__ import annotations

from collections import deque

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
    idx = int(round(q * (len(sorted_vals) - 1)))
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
        if ms != ms or ms in (float("inf"), float("-inf")):
            return
        series = self._series.get(name)
        if series is None:
            series = self._series[name] = deque(maxlen=self.maxlen)
        series.append(float(ms))

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
