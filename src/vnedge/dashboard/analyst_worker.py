"""Opt-in public research collector and deduplicated technical report history.

No credentials, exchange order client, strategy registry, or candle writes.
One process owns the evidence database; advisory lease refuses double writers.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vnedge.dashboard.analyst_public import collect_public
from vnedge.dashboard.analyst_store import AnalystStore, digest
from vnedge.dashboard.analyst_workspace import AnalystWorkspace
from vnedge.dashboard.crypto_analyst import EXCHANGES, TIMEFRAMES
from vnedge.dashboard.crypto_fundamentals import collect_fundamentals


def record_reports(workspace: AnalystWorkspace, store: AnalystStore) -> dict[str, Any]:
    now = datetime.now(UTC)
    counts = {"reports": 0, "changes": 0}
    for exchange in EXCHANGES:
        symbols: dict[str, dict[str, Any]] = {}
        for tf in TIMEFRAMES:
            for frame in workspace.core.snapshot(exchange, tf)["markets"]:
                symbols.setdefault(frame["symbol"], {})
                if frame.get("analysis_id"):
                    # Only the stable identity and facts, not poll-time age.
                    symbols.setdefault(frame["symbol"], {})[tf] = {
                        key: frame[key]
                        for key in (
                            "analysis_id",
                            "as_of",
                            "bias",
                            "alignment",
                            "setups",
                            "metrics",
                            "issues",
                        )
                    }
        for symbol, frames in symbols.items():
            scope = exchange + "/" + symbol
            stages = workspace.stages(exchange, symbol, now)
            eligible = [
                stage for stage in stages if stage.get("stage_id") and stage["state"] == "current"
            ]
            if eligible:
                # Independent version/clock ledger; never a strategy decision stream.
                store.append("stage_report", scope, {"stages": eligible, "can_trade": False}, now)
            if not frames:
                continue
            previous = store.read("report", scope, now=now)
            report = {
                "schema": "analyst_saved_report_v1",
                "exchange": exchange,
                "symbol": symbol,
                "frames": frames,
                "can_trade": False,
            }
            old = previous[0]["body"] if previous else None
            if old == report:
                continue
            ref = store.append("report", scope, report, now)
            counts["reports"] += 1
            before = (
                {tf: [f["bias"], f["setups"]] for tf, f in old["frames"].items()} if old else None
            )
            after = {tf: [f["bias"], f["setups"]] for tf, f in frames.items()}
            if before != after:
                event = {
                    "event": "baseline_recorded" if old is None else "technical_profile_changed",
                    "before": before,
                    "after": after,
                    "report_id": ref,
                    "previous_report_id": previous[0]["evidence_id"] if previous else None,
                    "change_id": digest([scope, before, after, ref]),
                    "can_trade": False,
                }
                store.append("change", scope, event, now)
                counts["changes"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-db", type=Path, default=Path("data/analyst/evidence.sqlite"))
    parser.add_argument("--candle-root", type=Path, default=Path("data/candles"))
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval_seconds < 60:
        parser.error("interval must be at least 60 seconds")
    args.evidence_db.parent.mkdir(parents=True, exist_ok=True)
    with args.evidence_db.with_suffix(".lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = AnalystStore(args.evidence_db, writable=True)
        workspace = AnalystWorkspace(args.candle_root, args.evidence_db)
        while True:
            started = time.monotonic()
            status = collect_public(store)
            try:
                status["history"] = record_reports(workspace, store)
                status["fundamentals"] = collect_fundamentals(store)
            except Exception as exc:  # noqa: BLE001 - persisted degraded worker status
                status["issues"].append("report_capture_failed:" + type(exc).__name__)
            status["generated_at"] = datetime.now(UTC).isoformat()
            store.append("collector", "delta_india", status, datetime.now(UTC))
            print(json.dumps(status), flush=True)
            if args.once:
                return
            time.sleep(max(1, args.interval_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
