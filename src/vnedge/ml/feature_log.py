"""Per-decision feature log — the bridge from live evaluations to training.

Correction-spec W5.1: every strategy evaluation (fired or not) appends the
EXACT feature vector the ML plane would have seen, so training later reads
this log instead of re-deriving features — train/serve skew is dead by
construction, and every row carries a fingerprint of the column contract so
a schema drift is detected at read time instead of silently poisoning a fit.

Durability contract, stated plainly: this is RESEARCH EVIDENCE, not order
safety. Rows are line-buffered and flushed but never fsynced — a crash may
lose the tail, which costs training rows, never money, and never adds an
event-loop stall to the decision path (the journal's fsync discipline exists
because losing an ORDER record is different in kind).

Fail-soft contract: ``append`` swallows every exception and returns False.
A feature-log failure can never affect a decision, a journal record, or a
lane's health.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix

logger = logging.getLogger(__name__)

FEATURE_LOG_SCHEMA_VERSION = 1


def feature_columns_fingerprint() -> str:
    """Short stable hash of the FEATURE_COLUMNS contract (order-sensitive)."""
    joined = ",".join(FEATURE_COLUMNS)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


class FeatureLogWriter:
    """Append one JSONL row per evaluation beside the lane's other artifacts."""

    def __init__(
        self,
        path: Path | str,
        *,
        strategy_id: str,
        symbol: str,
        timeframe: str,
        params: FeatureParams | None = None,
    ) -> None:
        self.path = Path(path)
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.params = params or FeatureParams()
        self.rows_written = 0
        self.errors = 0
        self._fingerprint = feature_columns_fingerprint()
        self._logged_error_types: set[str] = set()
        # One matrix computation serves every append on the same bar.
        self._cache_key: tuple[int, object] | None = None
        self._cache_frame: pd.DataFrame | None = None

    def _matrix_for(self, frame: pd.DataFrame) -> pd.DataFrame:
        key = (len(frame), frame["timestamp"].iloc[-1])
        if self._cache_key == key and self._cache_frame is not None:
            return self._cache_frame
        matrix = build_feature_matrix(frame, None, self.params)
        self._cache_key = key
        self._cache_frame = matrix
        return matrix

    def append(
        self,
        frame: pd.DataFrame,
        index: int,
        *,
        decision: str,
        bar_ts: str,
        intent_key: str | None = None,
        backfill: bool = False,
    ) -> bool:
        try:
            matrix = self._matrix_for(frame)
            row = matrix.iloc[index]
            features: dict[str, float | None] = {}
            for column in FEATURE_COLUMNS:
                value = row.get(column)
                number = float(value) if value is not None else math.nan
                features[column] = None if math.isnan(number) else number
            record = {
                "v": FEATURE_LOG_SCHEMA_VERSION,
                "fingerprint": self._fingerprint,
                "ts": datetime.now(UTC).isoformat(),
                "bar_ts": bar_ts,
                "strategy_id": self.strategy_id,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "decision": decision,
                "intent_key": intent_key,
                "backfill": bool(backfill),
                "features": features,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, allow_nan=False) + "\n")
                handle.flush()
            self.rows_written += 1
            return True
        except Exception as exc:  # noqa: BLE001 — fail-soft by contract
            self.errors += 1
            error_type = type(exc).__name__
            if error_type not in self._logged_error_types:
                self._logged_error_types.add(error_type)
                logger.warning(
                    "feature log append failed (%s: %s) — decisions unaffected",
                    error_type, exc,
                )
            return False


def read_feature_log(paths: list[Path] | tuple[Path, ...]) -> pd.DataFrame:
    """Load feature-log rows into a flat frame; refuse mixed fingerprints.

    A mixed-fingerprint log means the column contract changed mid-stream;
    training on it would silently blend incompatible feature spaces, so the
    reader fails loudly instead.
    """
    rows: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for path in paths:
        try:
            handle = Path(path).open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("v") != FEATURE_LOG_SCHEMA_VERSION:
                    continue
                fingerprints.add(str(record.get("fingerprint")))
                flat = {
                    key: record[key]
                    for key in ("ts", "bar_ts", "strategy_id", "symbol",
                                "timeframe", "decision", "intent_key", "backfill")
                }
                flat.update(record.get("features") or {})
                rows.append(flat)
    if len(fingerprints) > 1:
        raise ValueError(
            f"feature log mixes column contracts: {sorted(fingerprints)}"
        )
    return pd.DataFrame(rows)
