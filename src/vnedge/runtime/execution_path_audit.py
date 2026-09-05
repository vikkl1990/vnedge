"""Offline verifier for the kernel-only operational execution spine."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vnedge.execution.evidence import DecisionEnvelope
from vnedge.execution.journal import verify_journal_chain
from vnedge.runtime.canonical_parity import atomic_write_json
from vnedge.runtime.execution_contract import KERNEL_PATH_ID

SCHEMA = "vnedge.execution_path_audit.v1"


def _intent(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("intent")
    return value if isinstance(value, dict) else {}


def _valid_envelope(payload: dict[str, Any]) -> bool:
    evidence = payload.get("execution_evidence")
    if not isinstance(evidence, dict):
        return False
    envelope = evidence.get("arm_envelope")
    if not isinstance(envelope, dict):
        return False
    try:
        parsed = DecisionEnvelope.from_dict(envelope)
    except (KeyError, TypeError, ValueError):
        return False
    return (
        payload.get("path_id") == KERNEL_PATH_ID
        and payload.get("decision_id") == parsed.decision_id
        and evidence.get("decision_id") == parsed.decision_id
    )


def build_execution_path_audit(
    records: Iterable[dict[str, Any]],
    *,
    chain_ok: bool,
) -> dict[str, Any]:
    submitted = 0
    kernel_submitted = 0
    research_rows = 0
    entry_leaks: list[dict[str, str]] = []
    for record in records:
        kind = str(record.get("kind") or "")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind in {"shadow_intent", "shadow_outcome", "scalp_shadow_intent", "scalp_shadow_outcome"}:
            research_rows += 1
            if payload.get("performance_eligible") is True or payload.get("path_id") == KERNEL_PATH_ID:
                entry_leaks.append({"kind": kind, "reason": "research_row_claims_authority"})
            continue
        if kind != "order_submitted":
            continue
        submitted += 1
        intent = _intent(payload)
        if bool(intent.get("reduce_only")):
            # Emergency/recovery exits may lack entry evidence. They remain
            # safe but never make an execution-ready entry sample.
            continue
        if _valid_envelope(payload):
            kernel_submitted += 1
        else:
            entry_leaks.append(
                {
                    "kind": kind,
                    "client_order_id": str(payload.get("client_order_id") or ""),
                    "reason": "new_risk_without_kernel_envelope",
                }
            )
    blockers: list[str] = []
    if not chain_ok:
        blockers.append("journal_chain_invalid")
    if entry_leaks:
        blockers.append("operational_path_leak")
    if kernel_submitted < 1:
        blockers.append("kernel_submission_sample_missing")
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "execution_ready": not blockers,
        "blockers": blockers,
        "counts": {
            "submitted": submitted,
            "kernel_submitted": kernel_submitted,
            "research_rows": research_rows,
            "leaks": len(entry_leaks),
        },
        "leaks": entry_leaks[:100],
    }


def assert_execution_path_artifact(
    path: str | Path,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=2),
) -> dict[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(str(payload["generated_at"]))
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("execution path audit is unreadable") from exc
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("execution path audit timestamp is not timezone-aware")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    generated = generated.astimezone(UTC)
    if generated > current + timedelta(minutes=5) or current - generated > max_age:
        raise ValueError("execution path audit is stale or future-dated")
    if payload.get("schema") != SCHEMA or payload.get("execution_ready") is not True:
        raise ValueError("execution path audit is not ready")
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    if int(counts.get("kernel_submitted") or 0) < 1 or int(counts.get("leaks") or 0):
        raise ValueError("execution path audit lacks a clean kernel sample")
    if payload.get("blockers"):
        raise ValueError("execution path audit contains blockers")
    return payload


def _read(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    args = parser.parse_args()
    while True:
        journal = Path(args.journal)
        paths = sorted(journal.glob("*.jsonl")) if journal.is_dir() else [journal]
        chains = [(path, verify_journal_chain(path)) for path in paths]
        result = build_execution_path_audit(
            (row for path in paths for row in _read(path)),
            chain_ok=all(report.ok for _, report in chains),
        )
        result["chains"] = [
            {
                "path": str(path),
                "valid": report.ok,
                "records": report.records,
                "error": report.reason,
            }
            for path, report in chains
        ]
        atomic_write_json(args.output, result)
        print(json.dumps(result, sort_keys=True))
        if args.interval_seconds <= 0:
            return
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()


__all__ = ["SCHEMA", "assert_execution_path_artifact", "build_execution_path_audit"]
