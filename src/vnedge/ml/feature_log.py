"""Per-decision feature log — the bridge from live evaluations to training.

Correction-spec W5.1. Every strategy evaluation (fired or not) records the
feature vector for that decision so training later reads this log instead of
re-deriving features — train/serve skew is closed by construction.

Three properties this module must guarantee, each learned from PR-#395 review:

1. **Off the decision loop.** ``enqueue`` copies the complete bounded working
   frame through the decision row and hands it to a background worker; the
   snapshot and hands it to a background worker; the expensive
   ``build_feature_matrix`` and the file write run on that worker, never on the
   trading event loop. The decision path pays only a small bounded copy.
2. **Deterministic from recorded inputs.** The logged model-plane vector is
   exactly
   ``build_feature_matrix(snapshot, funding, params)`` on the immutable
   snapshot with the writer's real ``FeatureParams`` and whatever optional
   inputs (funding/OI/benchmark) were actually available — recorded in the
   fingerprint's contract so a Phase-5 server reproducing the same function
   with the same inputs yields an identical vector. No silent default drift.
3. **Joinable by identity.** Every row carries ``decision_id``, ``side``,
   ``exchange``, ``lane``, ``bar_ts`` and a ``decision_bar_hash``, so outcomes
   join strictly by decision identity rather than by approximate time.

Durability is deliberately weaker than the order journal (flush, no fsync):
losing a tail costs training rows, never money. Fail-soft by contract —
``enqueue`` and the worker swallow every exception, count it, and never raise
into a decision.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import queue
import threading
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix

logger = logging.getLogger(__name__)

FEATURE_LOG_SCHEMA_VERSION = 2
#: Bump when the feature MATH changes in a way params/columns don't capture.
FEATURE_CALC_VERSION = 1
_SENTINEL = object()


@dataclass(frozen=True)
class PreparedFeatures:
    """An off-loop, pre-signal vector; never backdated to the bar close."""

    captured_at: str
    bar_ts: str
    bar_hash: str
    fingerprint: str
    source_rows: int
    source_start: str
    values: tuple[tuple[str, float | None], ...]


def prepare_features(frame: pd.DataFrame, params: FeatureParams | None = None) -> PreparedFeatures:
    """Called on a worker before signal/entry. No disk, registry or execution IO."""
    params = params or FeatureParams()
    captured = datetime.now(UTC).isoformat()
    snapshot = frame.copy(deep=True)
    last = snapshot.iloc[-1]
    bar_hash = str(last.get("content_sha256") or "")
    if len(bar_hash) != 64 or any(c not in "0123456789abcdef" for c in bar_hash):
        raise ValueError("pre_entry_canonical_hash_required")
    matrix = build_feature_matrix(snapshot, None, params)
    values = []
    for column in FEATURE_COLUMNS:
        number = float(matrix.iloc[-1].get(column, math.nan))
        values.append((column, number if math.isfinite(number) else None))
    return PreparedFeatures(
        captured,
        pd.Timestamp(last["timestamp"]).isoformat(),
        bar_hash,
        feature_fingerprint(params),
        len(snapshot),
        str(snapshot["timestamp"].iloc[0]),
        tuple(values),
    )


def unavailable_features(features: dict, optional_inputs: list[str] | tuple[str, ...]) -> list[str]:
    """Distinguish missing feeds from the legacy neutral feature values."""
    absent = {name for name, value in features.items() if value is None}
    for source, names in (
        ("funding", ("funding_rate", "funding_pct", "funding_z")),
        ("open_interest", ("oi_z", "oi_change", "oi_price_div")),
        ("benchmark", ("rel_ret", "rel_strength_z")),
    ):
        if source not in optional_inputs:
            absent.update(names)
    if features.get("taker_flow_coverage") != 1:
        absent.update(("taker_buy_ratio", "delta_ratio", "delta_z", "cvd_slope", "delta_price_div"))
    return sorted(absent)


def _params_signature(params: FeatureParams) -> str:
    """Stable JSON of the (possibly nested) FeatureParams dataclass."""

    def _plain(value: Any) -> Any:
        if is_dataclass(value) and not isinstance(value, type):
            return {k: _plain(v) for k, v in asdict(value).items()}
        return value

    return json.dumps(_plain(params), sort_keys=True, default=str)


def feature_fingerprint(params: FeatureParams, optional_inputs: tuple[str, ...] = ()) -> str:
    """Fingerprint the full feature CONTRACT, not just column names.

    Covers ordered columns + serialized params + calc version + which optional
    inputs were present. Changing ``FeatureParams.vol_window`` (or the calc
    version, or losing funding) now changes the fingerprint — the #395-review
    gap where a params change kept an identical hash.
    """
    payload = "\n".join(
        [
            ",".join(FEATURE_COLUMNS),
            _params_signature(params),
            f"calc={FEATURE_CALC_VERSION}",
            "inputs=" + ",".join(sorted(optional_inputs)),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class FeatureLogWriter:
    """Append one JSONL row per evaluation from a background worker."""

    def __init__(
        self,
        path: Path | str,
        *,
        strategy_id: str,
        symbol: str,
        timeframe: str,
        exchange: str = "",
        lane_id: str = "",
        params: FeatureParams | None = None,
        funding: pd.DataFrame | None = None,
        max_queue: int = 4096,
    ) -> None:
        self.path = Path(path)
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.exchange = exchange
        self.lane_id = lane_id
        self.params = params or FeatureParams()
        self._funding = funding
        self._optional_inputs = ("funding",) if funding is not None else ()
        self.fingerprint = feature_fingerprint(self.params, self._optional_inputs)
        self.rows_written = 0
        self.errors = 0
        self.dropped = 0
        self._logged_error_types: set[str] = set()
        self._write_lock = threading.Lock()
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_queue)
        self._worker = threading.Thread(
            target=self._run, name=f"feature-log:{lane_id or strategy_id}", daemon=True
        )
        self._worker.start()

    def enqueue(
        self,
        frame: pd.DataFrame,
        index: int,
        *,
        decision: str,
        bar_ts: str,
        decision_bar_hash: str,
        decision_id: str | None = None,
        side: str | None = None,
        skip_reason: str | None = None,
        backfill: bool = False,
        prepared: PreparedFeatures | None = None,
    ) -> bool:
        """Copy the bounded working prefix and hand it to the worker.

        Exponential features retain memory older than ``warmup_bars``.  The
        caller already bounds the live working frame, so trimming it again
        here would create a second, non-equivalent feature series.

        Non-blocking: drops (counted) rather than block the event loop when the
        queue is full. Returns False on drop or any error.
        """
        try:
            if not decision_bar_hash:
                raise ValueError("canonical decision_bar_hash is required")
            snapshot = frame.iloc[: index + 1].copy(deep=True)
            local_index = index
            meta = {
                "captured_at": datetime.now(UTC).isoformat(),
                "bar_ts": bar_ts,
                "decision": decision,
                "decision_id": decision_id,
                "side": side,
                "skip_reason": skip_reason,
                "backfill": bool(backfill),
                "decision_bar_hash": decision_bar_hash,
                "source_row_count": len(snapshot),
                "source_start_ts": (
                    str(snapshot["timestamp"].iloc[0])
                    if "timestamp" in snapshot.columns and not snapshot.empty
                    else None
                ),
            }
            funding_snapshot = self._funding.copy(deep=True) if self._funding is not None else None
            if prepared is not None:
                if (
                    backfill
                    or decision != "fired"
                    or not decision_id
                    or side not in {"long", "short"}
                    or prepared.fingerprint != self.fingerprint
                    or prepared.bar_hash != decision_bar_hash
                    or pd.Timestamp(prepared.bar_ts) != pd.Timestamp(bar_ts)
                    or prepared.source_rows != len(snapshot)
                ):
                    raise ValueError("pre_entry_feature_identity_mismatch")
                meta["captured_at"] = prepared.captured_at
                # Only the small computed vector is synchronously persisted.
                # The caller submits an entry AFTER this returns. Never wait
                # for the background queue (which includes historical rows).
                self._write_one(
                    snapshot,
                    local_index,
                    meta,
                    funding_snapshot,
                    prepared_features=dict(prepared.values),
                    durable=True,
                )
                return True
            self._queue.put_nowait((snapshot, local_index, meta, funding_snapshot))
            return True
        except queue.Full:
            self.dropped += 1
            return False
        except Exception as exc:  # noqa: BLE001 — fail-soft by contract
            self._note_error(exc)
            return False

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                self._queue.task_done()
                return
            try:
                self._write_one(*item)
            except Exception as exc:  # noqa: BLE001 — fail-soft by contract
                self.errors += 1
                self._note_error(exc)
            finally:
                self._queue.task_done()

    def _write_one(
        self,
        snapshot: pd.DataFrame,
        index: int,
        meta: dict,
        funding_snapshot: pd.DataFrame | None = None,
        *,
        prepared_features: dict | None = None,
        durable: bool = False,
    ) -> None:
        features: dict[str, float | None] = {}
        if prepared_features is not None:
            features = prepared_features
        else:
            matrix = build_feature_matrix(snapshot, funding_snapshot, self.params)
            row = matrix.iloc[index]
            for column in FEATURE_COLUMNS:
                value = row.get(column)
                number = float(value) if value is not None else math.nan
                features[column] = None if not math.isfinite(number) else number
        record = {
            "v": FEATURE_LOG_SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "calc_version": FEATURE_CALC_VERSION,
            "ts": datetime.now(UTC).isoformat(),
            "captured_at": meta["captured_at"],
            "required_warmup_rows": self.params.warmup_bars,
            "warmup_complete": len(snapshot) >= self.params.warmup_bars,
            "missing_features": [name for name, value in features.items() if value is None],
            "unavailable_features": unavailable_features(features, self._optional_inputs),
            "bar_ts": meta["bar_ts"],
            "decision_id": meta["decision_id"],
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "exchange": self.exchange,
            "lane": self.lane_id,
            "side": meta["side"],
            "decision": meta["decision"],
            "skip_reason": meta["skip_reason"],
            "backfill": meta["backfill"],
            "decision_bar_hash": meta["decision_bar_hash"],
            "source_row_count": meta["source_row_count"],
            "source_start_ts": meta["source_start_ts"],
            "optional_inputs": list(self._optional_inputs),
            "features": features,
            "capture_path": "pre_entry_prepared_v1" if durable else "async_observation_v1",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, allow_nan=False) + "\n")
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        self.rows_written += 1

    def _note_error(self, exc: Exception) -> None:
        error_type = type(exc).__name__
        if error_type not in self._logged_error_types:
            self._logged_error_types.add(error_type)
            logger.warning("feature log %s (%s) — decisions unaffected", error_type, exc)

    def flush(self, timeout: float = 5.0) -> None:
        """Block until the queue drains (tests / clean checkpoints)."""
        done = threading.Event()

        def _wait() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=_wait, daemon=True).start()
        done.wait(timeout)

    def close(self, timeout: float = 5.0) -> None:
        """Stop the worker after draining what is queued."""
        try:
            self._queue.put(_SENTINEL, timeout=timeout)
        except queue.Full:
            self.dropped += 1
            return
        self._worker.join(timeout=timeout)


def read_feature_log(
    paths: list[Path] | tuple[Path, ...],
    *,
    expected_fingerprint: str | None = None,
) -> pd.DataFrame:
    """Load feature-log rows; refuse mixed or unexpected fingerprints.

    A mixed-fingerprint log blends incompatible feature spaces; the reader
    fails loudly. When ``expected_fingerprint`` is given, a single row not
    matching it is also refused.
    """
    rows: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for path in paths:
        try:
            with Path(path).open(encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("v") != FEATURE_LOG_SCHEMA_VERSION:
                continue
            fingerprints.add(str(record.get("fingerprint")))
            flat = {
                key: record.get(key)
                for key in (
                    "ts",
                    "bar_ts",
                    "decision_id",
                    "strategy_id",
                    "symbol",
                    "timeframe",
                    "exchange",
                    "lane",
                    "side",
                    "decision",
                    "skip_reason",
                    "backfill",
                    "decision_bar_hash",
                )
            }
            flat.update(record.get("features") or {})
            rows.append(flat)
    if len(fingerprints) > 1:
        raise ValueError(f"feature log mixes feature contracts: {sorted(fingerprints)}")
    if expected_fingerprint is not None and fingerprints and fingerprints != {expected_fingerprint}:
        raise ValueError(
            f"feature log fingerprint {sorted(fingerprints)} != expected {expected_fingerprint!r}"
        )
    return pd.DataFrame(rows)
