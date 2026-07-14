"""Research-only job ledger for Agent Gateway requests."""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

JOB_ID_RE = re.compile(r"^agj_[a-f0-9]{16}$")


def new_job_id() -> str:
    return f"agj_{secrets.token_hex(8)}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def create_backtest_job(
    *,
    jobs_dir: Path,
    agent: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "job_id": new_job_id(),
        "kind": "backtest_request",
        "status": "PENDING_RESEARCH_ONLY",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "created_by": agent,
        "request": request,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }
    path = jobs_dir / f"{job['job_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    with (jobs_dir / "jobs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(job, sort_keys=True) + "\n")
    return job


def read_job(jobs_dir: Path, job_id: str) -> dict[str, Any] | None:
    if not JOB_ID_RE.match(job_id):
        return None
    path = jobs_dir / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def list_jobs(jobs_dir: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    if not jobs_dir.exists():
        return []
    jobs: list[dict[str, Any]] = []
    for path in sorted(jobs_dir.glob("agj_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            jobs.append(payload)
    jobs.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return jobs[: max(1, min(limit, 500))]

