"""Read-only catalog for durable VNEDGE backtest reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vnedge.agent_gateway.jobs import list_jobs
from vnedge.backtest.report import REPORT_SCHEMA
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
    report = _report_from_document(job)
    run_value = report.get("run") if report else None
    overview_value = report.get("overview") if report else None
    run: dict[str, Any] = run_value if isinstance(run_value, dict) else {}
    overview: dict[str, Any] = overview_value if isinstance(overview_value, dict) else {}
    return {
        "run_id": str(run.get("run_id") or job.get("job_id") or "unknown"),
        "status": str(run.get("status") or job.get("status") or "UNKNOWN"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at") or run.get("generated_at"),
        "strategy_id": run.get("strategy_id") or request.get("strategy_id"),
        "exchange": run.get("exchange") or request.get("exchange"),
        "symbol": run.get("symbol") or request.get("symbol"),
        "timeframe": run.get("timeframe") or request.get("timeframe"),
        "net_profit_usd": overview.get("net_profit_usd"),
        "num_trades": overview.get("num_trades"),
        "has_report": report is not None,
        "blocked_reason": job.get("blocked_reason"),
        "error": job.get("error"),
        "execution": result.get("execution"),
    }


def load_backtest_lab(
    *,
    jobs_dir: Path,
    reports_dir: Path,
    artifact_dir: Path | None = None,
    selected_run_id: str | None = None,
) -> dict[str, Any]:
    """Join queued job state with completed canonical report artifacts."""
    documents: dict[str, dict[str, Any]] = {}
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
    selected_summary = next(
        (row for row in summaries if str(row["run_id"]) == chosen),
        None,
    )
    return {
        "lab_id": "vnedge_backtest_lab_v1",
        "selected_run_id": chosen,
        "selected": selected,
        "selected_summary": selected_summary,
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
