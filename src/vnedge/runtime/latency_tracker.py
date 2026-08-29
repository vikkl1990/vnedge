"""Rolling latency measurement for the tick-fast, bar-slow runtime.

The clocks are deliberately separate:

* ``ingest_lag_ms`` — exchange event timestamp to local receipt. This measures
  how quickly VNEDGE sees a tick; it never authorizes a strategy decision.
* ``bar_close_receipt_ms`` — canonical bucket close to local feed receipt.
  ``bar_close_processing_ms`` and ``feed_lag_ms`` are compatibility aliases
  for this same transport observation.
* ``canonical_wait_ms`` — bounded time spent waiting for the trade-derived
  candle lake after the exchange close notification arrives.
* ``decision_lag_ms`` — monotonic compute time from closed bar in hand through
  strategy preparation and signal evaluation.
* ``close_to_arm_ms`` — one correlated sample from canonical close boundary to
  the arm state update. This is measured directly; component percentiles are
  never added together.
* ``clock_skew_ms`` — magnitude by which an exchange timestamp is ahead of the
  receiving UTC clock. A future event is a data-quality observation, not a
  negative/fast latency sample.

Every metric in this module is report-only unless ``latency_thresholds`` names
it explicitly. Tick ingest freshness is observability and stop-management
input; it cannot make a closed-bar strategy evaluate early.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from math import isfinite

INGEST_LAG_MS = "ingest_lag_ms"
QUOTE_INGEST_MS = "quote_ingest_ms"
BAR_CLOSE_RECEIPT_MS = "bar_close_receipt_ms"
BAR_CLOSE_PROCESSING_MS = "bar_close_processing_ms"
FEED_LAG_MS = "feed_lag_ms"  # legacy alias for BAR_CLOSE_PROCESSING_MS
CANONICAL_WAIT_MS = "canonical_wait_ms"
DECISION_LAG_MS = "decision_lag_ms"
CLOSE_TO_ARM_MS = "close_to_arm_ms"
HTF_CONTEXT_WAIT_MS = "htf_context_wait_ms"
QUOTE_ON_QUOTE_MS = "quote_on_quote_ms"
ACCEPTANCE_HOLD_MS = "acceptance_hold_ms"
GATE_EVAL_MS = "gate_eval_ms"
SHADOW_JOURNAL_MS = "shadow_journal_ms"
TICK_STOP_MS = "tick_stop_ms"
TRADE_INGEST_MS = "trade_ingest_ms"
BASE_CLOSE_PUBLISH_MS = "base_close_publish_ms"
AGGREGATE_PUBLISH_MS = "aggregate_publish_ms"
PARQUET_PERSIST_MS = "parquet_persist_ms"
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

    def record_labeled(self, name: str, ms: float, **labels: str) -> None:
        """Record an aggregate sample and a deterministic labelled split.

        Labels are deliberately encoded as a second metric key so the existing
        checkpoint and dashboard schemas remain backwards compatible.  The
        aggregate series remains addressable by ``name`` for SLO work, while
        operators can distinguish, for example, a Parquet hit from a timeout
        without combining unrelated samples.
        """
        self.record(name, ms)
        if not labels:
            return
        normalized = ",".join(
            f"{key}={str(value).strip().lower()}" for key, value in sorted(labels.items())
        )
        self.record(f"{name}{{{normalized}}}", ms)

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

    def record_bar_receipt(
        self, close_time: datetime, received_at: datetime
    ) -> dict[str, float]:
        """Record closed-bucket arrival before any canonical-lake wait.

        ``close_time`` is the canonical boundary, not the last trade timestamp.
        Future closes are clamped out of latency and exposed through
        ``clock_skew_ms``. The legacy ``feed_lag_ms`` series receives the exact
        same sample during the migration window.
        """
        close = self._utc(close_time, label="close_time")
        received = self._utc(received_at, label="received_at")
        delta_ms = (received - close).total_seconds() * 1000.0
        lag_ms = max(0.0, delta_ms)
        skew_ms = max(0.0, -delta_ms)
        self.record(BAR_CLOSE_RECEIPT_MS, lag_ms)
        self.record(BAR_CLOSE_PROCESSING_MS, lag_ms)
        self.record(FEED_LAG_MS, lag_ms)
        if skew_ms > 0.0:
            self.record(CLOCK_SKEW_MS, skew_ms)
        return {
            BAR_CLOSE_RECEIPT_MS: lag_ms,
            BAR_CLOSE_PROCESSING_MS: lag_ms,
            CLOCK_SKEW_MS: skew_ms,
        }

    def record_bar_close(self, close_time: datetime, observed_at: datetime) -> dict[str, float]:
        """Compatibility wrapper for callers not yet renamed to receipt."""
        return self.record_bar_receipt(close_time, observed_at)

    def record_canonical_wait(self, elapsed_ms: float) -> None:
        """Record only bounded lake-wait duration, never transport latency."""
        self.record(CANONICAL_WAIT_MS, max(0.0, elapsed_ms))

    def stats(self, name: str) -> dict | None:
        """Summary plus a short ordered recovery tail for one metric.

        ``recent`` contains the five newest *recorded* samples.  Consumers use
        it to prove that a previously hard rolling percentile has recovered;
        unlike polling a snapshot, it cannot manufacture consecutive healthy
        observations without new bars actually reaching the decision loop.
        """
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
            "recent": [round(value, 2) for value in list(series)[-5:]],
        }

    def snapshot(self) -> dict:
        """All metrics -> their stats. Empty dict when nothing recorded yet."""
        out = {}
        for name in self._series:
            st = self.stats(name)
            if st is not None:
                out[name] = st
        return out

    def export_state(self) -> dict[str, object]:
        """Return the bounded raw samples needed to resume after a restart.

        Aggregated p95 values are deliberately insufficient for restoration:
        the arm gate and recovery proof need the real ordered tail.  The state
        contains no market, account, or order data and is safe to checkpoint as
        telemetry alongside the other per-lane runtime artifacts.
        """
        return {
            "version": 1,
            "bar_close_semantics": "receipt_v2",
            "maxlen": self.maxlen,
            "series": {name: list(values) for name, values in self._series.items()},
        }

    def restore_state(self, state: Mapping[str, object]) -> int:
        """Replace this tracker with a validated checkpoint.

        The current process owns ``maxlen``; a checkpoint cannot enlarge the
        runtime window.  Any malformed or non-finite value rejects the entire
        checkpoint so a partially corrupt file can never shape a safety gate.
        Returns the number of restored samples.
        """
        if state.get("version") != 1:
            raise ValueError("unsupported latency checkpoint version")
        raw_series = state.get("series")
        if not isinstance(raw_series, Mapping):
            raise TypeError("latency checkpoint series must be a mapping")
        legacy_close_semantics = state.get("bar_close_semantics") != "receipt_v2"

        restored: dict[str, deque[float]] = {}
        count = 0
        for raw_name, raw_values in raw_series.items():
            if not isinstance(raw_name, str) or not raw_name:
                raise ValueError("latency checkpoint metric name is invalid")
            if not isinstance(raw_values, list):
                raise TypeError(
                    f"latency checkpoint metric {raw_name!r} must be a list"
                )
            if legacy_close_semantics and raw_name in {
                BAR_CLOSE_PROCESSING_MS,
                FEED_LAG_MS,
                BAR_CLOSE_RECEIPT_MS,
            }:
                # Pre-v2 close samples included the bounded canonical-lake
                # wait. Mixing them with receipt-only samples would keep lanes
                # falsely blocked for an entire rolling window after deploy.
                continue
            values: list[float] = []
            for raw_value in raw_values:
                try:
                    value = float(raw_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"latency checkpoint metric {raw_name!r} is not numeric"
                    ) from exc
                if not isfinite(value):
                    raise ValueError(
                        f"latency checkpoint metric {raw_name!r} is non-finite"
                    )
                values.append(value)
            bounded = values[-self.maxlen :]
            if bounded:
                restored[raw_name] = deque(bounded, maxlen=self.maxlen)
                count += len(bounded)

        self._series = restored
        return count
