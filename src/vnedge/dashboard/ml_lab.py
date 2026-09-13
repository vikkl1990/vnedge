"""Read-only ML audit artifact projection. No fitting or model deserialization."""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from vnedge.ml.lab_audit import AUDIT_SCHEMA


def ml_lab_payload(path: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    result = {"schema": "ml_lab_view_v1", "served_at": now.isoformat(),
              "artifact_state": "MISSING", "worker_generated_at": None, "source_as_of": None,
              "worker_age_s": None, "source_age_s": None, "audit": None,
              "can_trade": False, "can_promote": False, "can_train": False}
    try:
        if path.is_symlink():
            raise ValueError("symlink")
        with path.open("rb") as handle:
            content = handle.read(2_000_001)
        if len(content) > 2_000_000:
            raise ValueError("oversize artifact")
        payload = json.loads(content)
        json.dumps(payload, allow_nan=False)
        if not isinstance(payload, dict):
            raise ValueError("invalid artifact")
        audit = payload.get("audit")
        if payload.get("audit_schema") != AUDIT_SCHEMA or not isinstance(audit, dict) or audit.get("schema") != AUDIT_SCHEMA:
            return {**result, "artifact_state": "LEGACY_UNVERIFIED"}
        stamp = datetime.fromisoformat(str(payload.get("generated_at")))
        if stamp.tzinfo is None:
            raise ValueError("ambiguous artifact timestamp")
        age = (now - stamp).total_seconds()
        if not math.isfinite(age) or age < -60:
            raise ValueError("future artifact")
        result.update(artifact_state="CURRENT" if age <= 7200 else "STALE",
                      worker_generated_at=stamp.isoformat(), worker_age_s=max(0, age))
        source = audit.get("source_as_of")
        if source:
            source_stamp = datetime.fromisoformat(str(source))
            if source_stamp.tzinfo is None:
                raise ValueError("ambiguous source timestamp")
            source_age = (now - source_stamp).total_seconds()
            if source_age < -60:
                raise ValueError("future source")
            result.update(source_as_of=source_stamp.isoformat(), source_age_s=max(0, source_age))
        result["audit"] = {**audit, "can_trade": False, "can_promote": False}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, OverflowError):
        result.update(artifact_state="INVALID", audit=None)
    return result
