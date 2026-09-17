"""Read-only last-N decision trace. Does not replay or change scanner state.

python -m vnedge.research.scanner_signal_gap --journal PATH --bars 96
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from vnedge.strategy.scanner_observability import rejection_category


def summarize(records: Iterable[Mapping[str, Any]], *, bars: int = 96) -> dict[str, Any]:
    """Count distinct forward decision bars, keeping the latest evaluation.

    Event counts cover the journal; gate counts cover only the selected window.
    Backfill and heartbeats cannot inflate the observed signal count.
    """
    if bars < 1:
        raise ValueError("bars must be positive")
    rows: OrderedDict[tuple[str, str, str], Mapping[str, Any]] = OrderedDict()
    kinds: Counter[str] = Counter()
    for record in records:
        kind = str(record.get("kind", "unknown"))
        kinds[kind] += 1
        payload = record.get("payload", {})
        if kind != "lane_eval" or not isinstance(payload, Mapping) or payload.get("backfill"):
            continue
        key = (str(payload.get("strategy_id")), str(payload.get("symbol")),
               str(payload.get("bar_ts")))
        rows[key] = payload
        rows.move_to_end(key)
        if len(rows) > bars:
            rows.popitem(last=False)
    trace: list[dict[str, Any]] = []
    primary: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    for row in rows.values():
        features = row.get("features") or {}
        reason = row.get("primary_failed_gate") or row.get("skip_reason")
        failed = row.get("all_failed_gates") or []
        outcome = "signal" if row.get("fired") else str(reason or "unexplained_no_signal")
        primary[outcome] += 1
        failures.update(set(str(gate) for gate in failed))
        trace.append({
            "bar_ts": row.get("bar_ts"), "strategy_id": row.get("strategy_id"),
            "symbol": row.get("symbol"), "fired": bool(row.get("fired")),
            "primary_failed_gate": reason, "reject_category": rejection_category(reason),
            "all_failed_gates": failed, "regime_reason": features.get("regime_reason"),
            "structure_reason": features.get("bos15_structure_health_reason"),
            "last_quality_reset_at": features.get("bos15_last_quality_reset_at"),
            "eligible_hours_since_reset": features.get("bos15_eligible_bars_since_reset"),
            "confirmed_highs": features.get("bos15_confirmed_high_count"),
            "confirmed_lows": features.get("bos15_confirmed_low_count"),
            "decision_hash": (row.get("data_source") or {}).get("decision_row_sha256"),
        })
    trace.sort(key=lambda row: datetime.fromisoformat(str(row["bar_ts"])))
    return {
        "scope": "last_distinct_forward_evaluations",
        "requested_bars": bars, "evaluations": len(trace),
        "signals": sum(row["fired"] for row in trace),
        "first_bar": trace[0]["bar_ts"] if trace else None,
        "last_bar": trace[-1]["bar_ts"] if trace else None,
        "primary_gate_counts": dict(primary), "all_gate_counts": dict(failures),
        "whole_journal_event_counts": dict(kinds), "trace": trace,
        "can_trade": False, "can_promote": False,
    }


def read_records(path: Path) -> Iterable[dict[str, Any]]:
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as exc:
                    raise ValueError(f"invalid journal JSON at {path}:{number}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--bars", type=int, default=96)
    args = parser.parse_args()
    print(json.dumps(summarize(read_records(args.journal), bars=args.bars), indent=2))


if __name__ == "__main__":
    main()
