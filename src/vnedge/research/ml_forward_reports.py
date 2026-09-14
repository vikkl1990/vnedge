"""Explicit report-only forward worker; no scanner, order or promotion writes."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from vnedge.execution.evidence import DecisionEnvelope
from vnedge.ml.lab_pipeline import load_plan, predict_report
from vnedge.ml.ledger_labels import digest, read_records, timestamp


def run_once(root: Path, plan_id: str, lane_dir: Path) -> dict:
    load_plan(root, plan_id)
    counts: Counter = Counter()
    envelopes: dict[str, dict[str, dict]] = {}
    features: dict[str, dict[str, dict]] = {}
    quarantined: set[str] = set()
    for path in sorted(lane_dir.glob("*.journal.jsonl")):
        try:
            journal = read_records(path, chain="journal")
            rows = read_records(
                path.with_name(path.name.replace(".journal.jsonl", ".features.jsonl"))
            )
            for r in journal:
                p = r["payload"]
                if not isinstance(p, dict):
                    counts["invalid_payload"] += 1
                    continue
                ev = p.get("execution_evidence") or {}
                if not isinstance(ev, dict):
                    quarantined.add(str(p.get("decision_id")))
                    counts["invalid_execution_evidence"] += 1
                    continue
                raw = (
                    p
                    if r["kind"] == "decision_armed"
                    else p.get("arm_envelope") or ev.get("arm_envelope")
                )
                if raw is None:
                    continue
                try:
                    if not isinstance(raw, dict):
                        raise TypeError("invalid_envelope")
                    envelope = DecisionEnvelope.from_dict(raw)
                    if (
                        p.get("decision_id", envelope.decision_id) != envelope.decision_id
                        or ev.get("decision_id", envelope.decision_id) != envelope.decision_id
                    ):
                        raise ValueError("conflicting_envelope")
                    envelopes.setdefault(envelope.decision_id, {})[digest(raw)] = raw
                except (KeyError, ValueError, TypeError):
                    quarantined.add(
                        str(
                            p.get("decision_id")
                            or ev.get("decision_id")
                            or (raw.get("decision_id") if isinstance(raw, dict) else None)
                        )
                    )
                    counts["invalid_envelope"] += 1
            for row in rows:
                if (
                    not row.get("ts")
                    or not 0 <= (datetime.now(UTC) - timestamp(row["ts"])).total_seconds() <= 120
                ):
                    continue
                if row.get("lane") != path.name.removesuffix(".journal.jsonl"):
                    counts["feature_lane_mismatch"] += 1
                    continue
                features.setdefault(str(row.get("decision_id")), {})[digest(row)] = row
        except (OSError, ValueError, KeyError, TypeError) as exc:
            counts[str(exc)] += 1
    for decision_id, proofs in envelopes.items():
        rows = features.get(decision_id, {})
        if decision_id in quarantined or len(proofs) != 1 or len(rows) != 1:
            counts["no_unique_forward_join"] += 1
            continue
        if (root / "predictions" / plan_id / f"{decision_id}.json").exists():
            counts["already_recorded"] += 1
            continue
        try:
            predict_report(root, plan_id, next(iter(rows.values())), next(iter(proofs.values())))
            counts["predictions_recorded"] += 1
        except (OSError, KeyError, ValueError, TypeError) as exc:
            counts[str(exc)] += 1
    return {
        "role": "report_only_post_arm",
        "counts": dict(counts),
        "can_trade": False,
        "can_promote": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("research/ml_lab"))
    parser.add_argument("--lane-dir", type=Path, default=Path("logs/paper_trials"))
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--interval-seconds", type=float, default=0)
    args = parser.parse_args(argv)
    if args.interval_seconds and args.interval_seconds < 5:
        parser.error("interval must be zero (once) or at least 5 seconds")
    while True:
        print(
            json.dumps(run_once(args.root, args.plan_id, args.lane_dir), allow_nan=False),
            flush=True,
        )
        if not args.interval_seconds:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
