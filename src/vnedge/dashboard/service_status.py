"""Read-only, freshness-qualified projection of the host watchdog report."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any


def service_status(path: Path, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    missing = {"status": "unknown", "age_seconds": None, "services": [], "can_trade": False}
    try:
        if path.stat().st_size > 256_000:
            return missing
        raw = json.loads(path.read_text())
        age = now - float(raw["generated_at"])
        if not math.isfinite(age) or age < 0:
            return missing
        fresh = age <= 180
        rows = []
        for name, row in sorted(raw["services"].items()):
            rows.append({"name": name, "running": row.get("running") is True,
                         "health": row.get("health", "unprobed") if fresh else "unknown",
                         "action": row.get("action", "none"),
                         "restart_attempts_1h": len(row.get("restarts", []))})
        return {"status": "fresh" if fresh else "stale", "age_seconds": round(age, 1),
                "services": rows, "can_trade": False}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return missing
