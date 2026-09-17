"""Report capture liveness separately from historical completeness and readiness."""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any


def capture_health(root: Path, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    result: dict[str, Any] = {"recording": False, "coverage": "UNKNOWN", "scanner_ready": None}
    try:
        state = json.loads((root / "reports/delta_capture.json").read_text())
        age = now - float(state["updated_at"])
        rows = state["symbols"]
        fresh = math.isfinite(age) and 0 <= age <= 20 and bool(rows)
        healthy = fresh
        for symbol in ("BTCUSD", "ETHUSD"):
            row = rows.get(symbol, {})
            trade_age = now - float(row.get("last_trade_ms", 0)) / 1000
            row["recording"] = fresh and row.get("connected") is True and 0 <= trade_age <= 60
            healthy = healthy and row["recording"]
        result.update(state, recording=bool(healthy), status_age_seconds=age)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result["reason"] = "capture_status_missing_or_invalid"
    try:
        plan = json.loads((root / "reports/delta_recovery_plan.json").read_text())
        from datetime import datetime
        age = now - datetime.fromisoformat(plan["generated_at"]).timestamp()
        statuses = [p.get("status") for p in plan["symbols"].values()]
        result["coverage"] = (
            "STALE" if not 0 <= age <= 1800 else
            "VERIFIED_WINDOW" if len(statuses) >= 2 and all(s == "VERIFIED_WINDOW" for s in statuses)
            else "GAPS_OR_ERRORS"
        )
        result["coverage_window"] = plan
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    # These gauges cannot authorize scanner readiness or trading.
    result["scanner_ready"] = None
    result["scanner_readiness_source"] = "per_lane_gates"
    return result


def save_capture(root: Path, symbols: list[str], *, connected: bool,
                 last_trades: dict[str, int]) -> None:
    path = root / "reports/delta_capture.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": time.time(), "symbols": {
        s: {"connected": connected, "last_trade_ms": last_trades.get(s, 0)} for s in symbols}}
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload))
    os.replace(temp, path)


async def monitor_capture(root: Path) -> None:
    """Dedicated notifier task: network notification never blocks ingestion."""
    import asyncio
    from vnedge.monitoring.alerts import AlertEngine, AlertRule
    from vnedge.monitoring.notifiers import LogNotifier, TelegramNotifier
    telegram = TelegramNotifier.from_env()
    engine = AlertEngine([
        AlertRule("delta_capture_unavailable", "critical",
                  lambda s: not s["recording"],
                  lambda s: "Delta per-symbol capture unavailable/stale. See delta_capture.json; no coverage inferred.", 300),
        AlertRule("delta_coverage_unresolved", "critical",
                  lambda s: s["coverage"] != "VERIFIED_WINDOW",
                  lambda s: "Delta historical coverage: " + s["coverage"] + ". Reconnect is not historical recovery.", 1800),
    ], root / "reports/delta_capture_alerts.jsonl", [LogNotifier(), *([telegram] if telegram else [])])
    await asyncio.sleep(20)
    while True:
        await asyncio.to_thread(engine.evaluate, capture_health(root))
        await asyncio.sleep(5)


if __name__ == "__main__":
    import sys
    report = capture_health(Path("data"))
    print(json.dumps(report))
    sys.exit(0 if report["recording"] else 1)
