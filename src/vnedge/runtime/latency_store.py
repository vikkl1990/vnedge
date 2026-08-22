"""Atomic per-lane latency checkpoints.

The rolling latency tracker used to restart at zero on every deploy.  That made
5m lanes display ``warming`` for ~100 minutes and 1h lanes for ~20 hours even
though their candle cache, account, journals, and funnel had all resumed.  This
store applies the same incremental contract as those artifacts: restore the
bounded ordered samples, then append only newly observed closed-bar samples.

Latency can participate in the new-arm gate, so restoration is stricter than a
display counter: lane identity, schema version, numeric values, and finiteness
must all validate.  A missing/corrupt checkpoint is ignored and the tracker
collects fresh evidence; exits and risk controls are never affected.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from vnedge.runtime.latency_tracker import LatencyTracker

logger = logging.getLogger(__name__)


class LaneLatencyStore:
    """Persist and restore one lane's bounded raw latency samples."""

    def __init__(self, path: Path | str, lane_id: str) -> None:
        self.path = Path(path)
        self.lane_id = lane_id

    def save_from(self, tracker: LatencyTracker) -> None:
        payload = {
            "schema_version": 1,
            "lane_id": self.lane_id,
            "saved_at": datetime.now(UTC).isoformat(),
            "tracker": tracker.export_state(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(f"{self.path}.tmp")
            tmp.write_text(json.dumps(payload, allow_nan=False, separators=(",", ":")))
            tmp.replace(self.path)
        except (OSError, TypeError, ValueError) as exc:
            # Checkpointing must never take down a lane or interfere with exits.
            logger.warning(
                "latency checkpoint save failed for %s: %s", self.lane_id, exc
            )

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "latency checkpoint unreadable for %s: %s", self.lane_id, exc
            )
            return None
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            logger.warning("latency checkpoint schema invalid for %s", self.lane_id)
            return None
        if payload.get("lane_id") != self.lane_id:
            logger.warning(
                "latency checkpoint at %s belongs to %r, not %r — ignoring",
                self.path,
                payload.get("lane_id"),
                self.lane_id,
            )
            return None
        tracker = payload.get("tracker")
        if not isinstance(tracker, dict):
            logger.warning("latency checkpoint payload invalid for %s", self.lane_id)
            return None
        return tracker

    def restore_into(self, tracker: LatencyTracker) -> bool:
        state = self.load()
        if state is None:
            return False
        try:
            restored = tracker.restore_state(state)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "latency checkpoint rejected for %s: %s", self.lane_id, exc
            )
            return False
        logger.info(
            "resumed latency for %s: %d bounded samples", self.lane_id, restored
        )
        return True
