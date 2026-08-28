"""Read-only catalog for durable VNEDGE backtest reports."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from vnedge.agent_gateway.jobs import list_jobs
from vnedge.backtest.report import REPORT_SCHEMA
from vnedge.research.evidence_bundle import list_bundle_index, load_bundle
from vnedge.strategy.strategy_registry import STRATEGIES


def _read(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _report_from_document(document: dict[str, Any]) -> dict[str, Any] | None:
    if document.get("schema") == REPORT_SCHEMA:
        return document
    direct = document.get("backtest_report")
    if isinstance(direct, dict) and direct.get("schema") == REPORT_SCHEMA:
        return direct
    result = document.get("result")
    if isinstance(result, dict):
        report = result.get("backtest_report")
        if isinstance(report, dict) and report.get("schema") == REPORT_SCHEMA:
            return report
    return None


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    request_value = job.get("request")
    result_value = job.get("result")
    request: dict[str, Any] = request_value if isinstance(request_value, dict) else {}
    result: dict[str, Any] = result_value if isinstance(result_value, dict) else {}
    bundle_value = job.get("evidence_bundle")
    bundle: dict[str, Any] = bundle_value if isinstance(bundle_value, dict) else {}
    report = _report_from_document(job)
    run_value = report.get("run") if report else None
    overview_value = report.get("overview") if report else None
    run: dict[str, Any] = run_value if isinstance(run_value, dict) else {}
    overview: dict[str, Any] = overview_value if isinstance(overview_value, dict) else {}
    bundle_metrics_value = bundle.get("metrics")
    bundle_metrics: dict[str, Any] = (
        bundle_metrics_value if isinstance(bundle_metrics_value, dict) else {}
    )
    return {
        "run_id": str(
            run.get("run_id") or bundle.get("run_id") or job.get("job_id") or "unknown"
        ),
        "status": str(run.get("status") or job.get("status") or "UNKNOWN"),
        "created_at": job.get("created_at"),
        "updated_at": (
            job.get("updated_at") or run.get("generated_at") or bundle.get("generated_at")
        ),
        "strategy_id": (
            run.get("strategy_id") or bundle.get("strategy_id") or request.get("strategy_id")
        ),
        "exchange": run.get("exchange") or bundle.get("exchange") or request.get("exchange"),
        "symbol": run.get("symbol") or bundle.get("symbol") or request.get("symbol"),
        "timeframe": (
            run.get("timeframe")
            or next(iter(bundle.get("timeframes") or ()), None)
            or request.get("timeframe")
        ),
        "net_profit_usd": overview.get("net_profit_usd", bundle_metrics.get("net_profit_usd")),
        "num_trades": overview.get("num_trades", bundle_metrics.get("num_trades")),
        "has_report": report is not None or bool(bundle),
        "blocked_reason": job.get("blocked_reason"),
        "error": job.get("error"),
        "execution": result.get("execution"),
        "bundle_id": bundle.get("bundle_id"),
        "parity_status": (bundle.get("engine") or {}).get("parity_status")
        if isinstance(bundle.get("engine"), dict)
        else None,
        "code_sha": bundle.get("code_sha"),
    }


def load_backtest_lab(
    *,
    jobs_dir: Path,
    reports_dir: Path,
    artifact_dir: Path | None = None,
    evidence_bundle_dir: Path | None = Path("research/evidence_bundles"),
    evidence_index_path: Path | None = Path("research/evidence_bundles/index.sqlite"),
    selected_run_id: str | None = None,
) -> dict[str, Any]:
    """Join queued job state with completed canonical report artifacts."""
    documents: dict[str, dict[str, Any]] = {}
    bundle_paths: dict[str, Path] = {}
    evidence_catalog: dict[str, Any] = {
        "state": "MISSING",
        "indexed": 0,
        "verified": 0,
        "invalid": 0,
    }
    for job in list_jobs(jobs_dir, limit=500):
        run_id = str(job.get("job_id") or "")
        if run_id:
            documents[run_id] = job

    for root in (reports_dir, artifact_dir):
        if root is None or not root.exists():
            continue
        for path in sorted(root.glob("*.json")):
            payload = _read(path)
            if payload is None:
                continue
            report = _report_from_document(payload)
            run = report.get("run", {}) if report else {}
            run_id = str(run.get("run_id") or payload.get("job_id") or path.stem)
            if run_id not in documents or report is not None:
                documents[run_id] = payload

    if evidence_bundle_dir is not None and evidence_index_path is not None:
        try:
            bundle_rows = list_bundle_index(evidence_index_path, limit=500)
            evidence_catalog["state"] = "OK" if evidence_index_path.exists() else "MISSING"
            evidence_catalog["indexed"] = len(bundle_rows)
        except (OSError, sqlite3.Error) as exc:
            bundle_rows = []
            evidence_catalog.update({"state": "UNREADABLE", "error": str(exc)})
        for row in bundle_rows:
            bundle_path = Path(str(row.get("bundle_path") or ""))
            if not bundle_path.is_absolute():
                # Indexes written inside a container may retain a relative path;
                # the bundle id remains the portable lookup key.
                bundle_path = evidence_bundle_dir / str(row.get("bundle_id") or "")
            try:
                manifest_value = json.loads(str(row.get("manifest_json") or "{}"))
            except json.JSONDecodeError:
                evidence_catalog["invalid"] += 1
                continue
            if not isinstance(manifest_value, dict) or not manifest_value.get("run_id"):
                evidence_catalog["invalid"] += 1
                continue
            run_id = str(manifest_value["run_id"])
            bundle_paths[run_id] = bundle_path
            documents[run_id] = {
                "status": "COMPLETE",
                "updated_at": manifest_value.get("generated_at"),
                "evidence_bundle": manifest_value,
            }

    summaries = [_job_summary(document) for document in documents.values()]
    summaries.sort(
        key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""),
        reverse=True,
    )
    reports = {
        str((_report_from_document(document) or {}).get("run", {}).get("run_id")):
        _report_from_document(document)
        for document in documents.values()
        if _report_from_document(document) is not None
    }
    known_ids = {str(row["run_id"]) for row in summaries}
    chosen = selected_run_id if selected_run_id in known_ids else None
    if chosen is None:
        chosen = next(
            (str(row["run_id"]) for row in summaries if row.get("has_report")),
            None,
        )
    selected = reports.get(chosen) if chosen else None
    selected_document = documents.get(chosen, {}) if chosen else {}
    selected_evidence = (
        selected_document.get("evidence_bundle")
        if isinstance(selected_document, dict)
        else None
    )
    if selected is None and chosen in bundle_paths:
        try:
            verified_manifest, selected = load_bundle(bundle_paths[chosen])
            selected_evidence = verified_manifest.model_dump(mode="json")
            evidence_catalog["verified"] += 1
        except (OSError, ValueError, json.JSONDecodeError):
            evidence_catalog["invalid"] += 1
            selected = None
    selected_summary = next(
        (row for row in summaries if str(row["run_id"]) == chosen),
        None,
    )
    return {
        "lab_id": "vnedge_backtest_lab_v1",
        "selected_run_id": chosen,
        "selected": selected,
        "selected_summary": selected_summary,
        "selected_evidence": selected_evidence,
        "evidence_catalog": evidence_catalog,
        "runs": summaries[:200],
        "catalog": {
            "strategies": sorted(STRATEGIES),
            "exchanges": ["binanceusdm", "bybit", "delta_india"],
            "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
            "timeframes": ["5m", "15m", "1h", "4h"],
        },
        "submission": {
            "mode": "AGENT_GATEWAY_JOB",
            "inline_execution": False,
            "reason": (
                "Backtests run in the bounded research worker, never inside the "
                "dashboard request process."
            ),
            "worker_command": "python -m vnedge.agent_gateway.job_runner --once --json",
        },
        "read_only": True,
        "can_trade": False,
        "can_promote": False,
    }
