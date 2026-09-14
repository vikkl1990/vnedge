"""Bounded, read-only audit of recorded model features and outcome candidates.

This is deliberately NOT a trainer or a candle fallback. A matching decision ID
is useful evidence, but cannot prove final entry/exit accounting or a prediction
available at decision time. Those independent checks remain explicit blockers.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vnedge.execution.evidence import DecisionEnvelope
from vnedge.ml.feature_matrix import FEATURE_COLUMNS

AUDIT_SCHEMA = "ml_dataset_audit_v1"
MAX_FILES = 64
READ_BYTES = 1_048_576
OUTCOMES = {"live_paper_exit", "tick_stop_exit", "paper_exit"}
RESEARCH_OUTCOMES = {"shadow_outcome", "scalp_shadow_outcome"}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _stamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _tail(path: Path) -> tuple[list[dict], dict]:
    status = {"file": path.name, "state": "ok", "truncated": False, "invalid": 0, "incomplete_tail": False}
    if path.is_symlink():
        return [], {**status, "state": "symlink_refused"}
    rows: list[dict] = []
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - READ_BYTES)
            status["truncated"] = bool(start)
            stream.seek(start)
            if start:
                stream.readline(READ_BYTES)
            raw = stream.read(max(0, size - stream.tell()))
        status["incomplete_tail"] = bool(raw and not raw.endswith(b"\n"))
        raw = raw[:raw.rfind(b"\n") + 1]
        for line in raw.splitlines():
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("not an object")
                _hash(row)  # reject NaN/Infinity, including nested payloads
                rows.append(row)
            except (ValueError, TypeError, UnicodeError):
                status["invalid"] += 1
    except OSError:
        status["state"] = "unavailable"
    return rows, status


def audit_records(journals: dict[str, list[dict]], feature_logs: dict[str, list[dict]]) -> dict:
    """Inspect exact joins; never certify cash accounting from exit intent rows."""
    counts: Counter = Counter()
    exclusions: Counter = Counter()
    missing: Counter = Counter()
    cohorts: dict[tuple, dict] = {}
    features: dict[tuple, list[dict]] = defaultdict(list)
    envelopes: dict[tuple, dict[str, DecisionEnvelope]] = defaultdict(dict)
    conflicted: set[tuple[str, str]] = set()
    outcomes: list[tuple[str, dict]] = []
    event_times: list[datetime] = []
    source_ids: list[str] = []
    seen: set[str] = set()
    for lane, records in feature_logs.items():
        for row in records:
            identity = _hash([lane, "features", row])
            if identity in seen:
                counts["duplicate_feature_records"] += 1
                continue
            seen.add(identity)
            source_ids.append(identity)
            counts["feature_rows"] += 1
            stamp = _stamp(row.get("ts"))
            if stamp:
                event_times.append(stamp)
            key = tuple(str(row.get(k) or "unreported") for k in
                        ("strategy_id", "exchange", "symbol", "timeframe", "fingerprint"))
            cohort = cohorts.setdefault(key, dict(zip(
                ("strategy_id", "exchange", "symbol", "timeframe", "feature_fingerprint"), key)) |
                {"rows": 0, "fired_rows": 0, "complete_feature_rows": 0, "missing_values": 0,
                 "operational_labels": 0, "trainable": False})
            cohort["rows"] += 1
            cohort["fired_rows"] += row.get("decision") == "fired"
            values = row.get("features") if isinstance(row.get("features"), dict) else {}
            absent = [c for c in FEATURE_COLUMNS if not _finite(values.get(c)) or c in row.get("unavailable_features", [])]
            missing.update(absent)
            cohort["missing_values"] += len(absent)
            cohort["complete_feature_rows"] += not absent
            if row.get("v") != 2 or not row.get("fingerprint"):
                exclusions["feature_contract_missing"] += 1
                continue
            if row.get("backfill") is not False:
                exclusions["feature_backfill_or_unknown"] += 1
                continue
            if row.get("decision") != "fired" or not row.get("decision_id"):
                continue
            features[(lane, str(row["decision_id"]))].append(row)
    for lane, records in journals.items():
        for record in records:
            identity = _hash([lane, "journal", record])
            if identity in seen:
                counts["duplicate_journal_records"] += 1
                continue
            seen.add(identity)
            source_ids.append(identity)
            counts["journal_records"] += 1
            p = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            kind = record.get("kind")
            if not isinstance(kind, str):
                exclusions["invalid_journal_kind"] += 1
                continue
            stamp = _stamp(record.get("ts"))
            if stamp:
                event_times.append(stamp)
            if kind == "lane_eval":
                counts["evaluations"] += 1
            if kind in RESEARCH_OUTCOMES:
                counts["research_outcomes"] += 1
                exclusions["research_outcome_not_operational"] += 1
            if kind in OUTCOMES:
                outcomes.append((lane, p))
                counts["exit_records"] += 1
            ev = p.get("execution_evidence") if isinstance(p.get("execution_evidence"), dict) else {}
            raw = p.get("arm_envelope") or ev.get("arm_envelope")
            if raw is not None:
                try:
                    envelope = DecisionEnvelope.from_dict(raw)
                    if any(part.get("decision_id") not in (None, "", envelope.decision_id) for part in (p, ev)):
                        raise ValueError("conflicting identity")
                    envelopes[(lane, envelope.decision_id)][_hash(envelope.as_dict())] = envelope
                except (TypeError, ValueError, KeyError, AttributeError):
                    exclusions["invalid_envelope"] += 1
                    claimed = p.get("decision_id") or ev.get("decision_id")
                    if isinstance(claimed, str):
                        conflicted.add((lane, claimed))
    counts["bound_decisions"] = sum(len(proofs) == 1 and key not in conflicted for key, proofs in envelopes.items())
    for key, rows in features.items():
        if len(rows) != 1:
            exclusions["conflicting_feature_rows"] += 1
            continue
        proofs = envelopes.get(key, {})
        if len(proofs) != 1 or key in conflicted:
            exclusions["feature_without_unique_envelope"] += 1
            continue
        row, envelope = rows[0], next(iter(proofs.values()))
        checks = (("symbol", envelope.symbol), ("strategy_id", envelope.strategy_id),
                  ("timeframe", envelope.timeframe), ("side", envelope.side),
                  ("decision_bar_hash", envelope.decision_bar_content_hash))
        if row.get("lane") != key[0] or any(row.get(k) != expected for k, expected in checks) or _stamp(row.get("bar_ts")) != envelope.bar_open:
            exclusions["feature_identity_mismatch"] += 1
            continue
        counts["exact_feature_matches"] += 1
        # The current logger runs after the decision in a background worker.
        # Its persisted timestamp cannot prove a live model prediction existed.
        if not _stamp(row.get("ts")) or _stamp(row["ts"]) > envelope.permission_snapshot.decision_bar.close_time:
            exclusions["post_decision_feature_not_live_prediction"] += 1
    for lane, outcome in outcomes:
        key = (lane, str(outcome.get("decision_id") or ""))
        if not key[1] or len(envelopes.get(key, {})) != 1:
            exclusions["exit_without_bound_decision"] += 1
        if outcome.get("final") is not True or str(outcome.get("state", "")).lower() != "filled":
            exclusions["exit_not_final_filled"] += 1
        if not _finite(outcome.get("net_usd")):
            exclusions["exit_missing_after_cost_net"] += 1
        # These journal events describe exit submissions, not the reconciled
        # full-position entry/exit/fee/funding ledger. No flag can upgrade them.
        exclusions["resolved_ledger_label_not_bound"] += 1
    return {
        "schema": AUDIT_SCHEMA, "evidence_hash": _hash(sorted(source_ids)),
        "counts": {k: counts[k] for k in ("journal_records", "evaluations", "bound_decisions", "feature_rows",
            "exact_feature_matches", "research_outcomes", "exit_records", "duplicate_feature_records", "duplicate_journal_records")},
        "operational_labels": 0, "label_status": "BOUNDED_AUDIT_NOT_A_LABEL_SOURCE",
        "cohorts": [cohorts[k] for k in sorted(cohorts)],
        "feature_missingness": [{"feature": c, "missing_rows": missing[c]} for c in FEATURE_COLUMNS],
        "exclusions": dict(sorted(exclusions.items())),
        "source_as_of": max(event_times).isoformat() if event_times else None,
        "training": {"status": "BLOCKED", "trainable": False, "min_labels": 200,
            "cpcv_min_labels": 300, "min_train_fold_rows": 200,
            "blockers": ["resolved_ledger_labels_required", "cohort_cost_and_clock_binding_required",
                         "preregistered_event_time_splits_required"], "automatic_training": False},
        "validation": {"status": "NOT_RUN", "calibration_status": "NOT_FITTED", "passed": False,
                       "auc": None, "brier": None, "oos_net_usd": None},
        "models": [], "predictions": [], "can_trade": False, "can_promote": False,
    }


def build_ml_lab_audit(lane_dir: Path) -> dict:
    paths = sorted(lane_dir.glob("*.features.jsonl")) + sorted(lane_dir.glob("*.journal.jsonl"))
    logs: dict[str, list[dict]] = {}
    journals: dict[str, list[dict]] = {}
    sources = []
    for path in paths[:MAX_FILES]:
        rows, status = _tail(path)
        sources.append(status)
        if path.name.endswith(".features.jsonl"):
            logs[path.name.removesuffix(".features.jsonl")] = rows
        else:
            journals[path.name.removesuffix(".journal.jsonl")] = rows
    result = audit_records(journals, logs)
    return {**result, "sources": sources, "files_truncated": len(paths) > MAX_FILES,
            "source_directory_available": lane_dir.is_dir(), "history_complete": False,
            "coverage": "bounded_recent_audit", "max_bytes_per_file": READ_BYTES,
            "max_files": MAX_FILES}
