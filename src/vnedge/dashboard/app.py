"""Read-only dashboard server (docs/DESIGN.md §6).

Hard invariants, enforced structurally:
- No token, no dashboard: `create_app` refuses to start without at least one
  authorized user (legacy shared token or per-user store — see auth.py and
  docs/DASHBOARD_AUTH.md).
- Zero control actions: the only routes are the static page, GET /state,
  and the snapshot WebSocket. There is nothing to POST to.
- Cannot slow the bot: the server only reads whatever snapshot the bot last
  published; a dead or slow browser drops its own socket and nothing else.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import socket
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from vnedge.agent_gateway.app import (
    AgentGatewayArtifacts,
    env_agent_audit_path,
    env_agent_jobs_dir,
    mount_agent_gateway,
)
from vnedge.agent_gateway.audit import AgentAuditLogger
from vnedge.agent_gateway.auth import AgentTokenStore
from vnedge.agent_gateway.jobs import (
    BLOCKED_STATUS,
    DONE_STATUS,
    FAILED_STATUS,
    PENDING_STATUS,
    RUNNING_STATUS,
    TERMINAL_STATUSES,
    list_jobs,
)
from vnedge.agent_gateway.task_registry import (
    QuantOSAgentGateway,
    env_quant_os_agent_gateway_dir,
    quant_os_event_stream,
)
from vnedge.dashboard.auth import (
    AuthResult,
    DashboardUser,
    TokenStore,
    has_permission,
    permissions_for,
)
from vnedge.dashboard.trade_journal import build_trade_journal
from vnedge.research.external_repo_synthesis import build_external_repo_synthesis
from vnedge.research.pine_script_research import load_pine_research_payload
from vnedge.research.quantified_port_factory import load_quantified_port_factory_payload
from vnedge.research.quantified_pullback_reversion_proof import (
    load_quantified_pullback_reversion_proof_payload,
)
from vnedge.research.quantified_strategy_lab import load_quantified_strategy_lab_payload

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_START = time.time()


def _build_sha() -> str:
    for p in (Path("/app/BUILD_SHA"), _REPO_ROOT / "BUILD_SHA"):
        try:
            sha = p.read_text().strip()
            if sha:
                return sha
        except OSError:
            continue
    return "dev"

# --- incident timeline --------------------------------------------------------
# Journal kinds that are operator incidents (not routine order flow), mapped to
# a severity and a runbook anchor in docs/RUNBOOKS.md.
_INCIDENT_JOURNAL_KINDS: dict[str, tuple[str, str]] = {
    "reconciliation_fail_closed": ("critical", "reconciliation-fail-closed"),
    "orphaned_paper_position": ("warning", "orphaned-paper-position"),
    "plan_restore_rejected": ("warning", "plan-restore-rejected"),
    "emergency_flatten_started": ("critical", "kill-switch-and-flatten"),
    "emergency_flatten_finished": ("info", "kill-switch-and-flatten"),
}

# Alert rule_ids -> runbook anchors. Anything unmapped gets general triage.
_ALERT_RUNBOOKS: dict[str, str] = {
    "feed_stale": "feed-stale",
    "kill_switch": "kill-switch-and-flatten",
    "journal_unhealthy": "journal-unavailable",
    "risk_status": "risk-status-degraded",
    "daily_loss": "daily-loss-stop",
    "loss_streak": "loss-streak",
    "drawdown": "drawdown",
}
_GENERAL_RUNBOOK = "general-triage"

# Alert rule_ids that are trade notifications, not incidents.
_NON_INCIDENT_ALERTS = frozenset({"new_fill"})

_LANE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Human-facing venue names for the fee/PnL calculator (keys are the registry's
# canonical exchange ids).
_EXCHANGE_LABELS: dict[str, str] = {
    "binanceusdm": "Binance USDⓈ-M",
    "bybit": "Bybit V5",
    "delta_india": "Delta India",
}


def _safe_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _agent_job_adapter(job: dict) -> str:
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    params = request.get("parameters") if isinstance(request.get("parameters"), dict) else {}
    strategy_id = str(request.get("strategy_id") or "")
    adapter = str(params.get("adapter") or params.get("job_adapter") or "")
    if strategy_id.startswith("ai_"):
        return "ai_candidate"
    if "candidate_replay" in {strategy_id, adapter} or strategy_id == "candidate_replay_executor_v1":
        return "candidate_replay"
    return "registered_backtest"


def _agent_job_result_summary(job: dict) -> str:
    if job.get("blocked_reason"):
        return str(job["blocked_reason"])
    if job.get("error"):
        return str(job["error"])
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    if metrics:
        net = _safe_float(metrics.get("net_profit_usd"))
        trades = int(_safe_float(metrics.get("num_trades")) or 0)
        if net is not None:
            return f"net {net:+.2f} USD / trades {trades}"
        return f"trades {trades}"
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    if summary:
        candidates = int(_safe_float(summary.get("replay_candidates")) or 0)
        fills = int(_safe_float(summary.get("fills")) or 0)
        rows = int(_safe_float(summary.get("rows")) or 0)
        return f"replay candidates {candidates} / fills {fills} / rows {rows}"
    matched = result.get("matched_candidate")
    if isinstance(matched, dict):
        verdict = str(matched.get("verdict") or "candidate")
        net = _safe_float(matched.get("oos_net_usd"))
        return f"{verdict} {net:+.2f} USD" if net is not None else verdict
    if job.get("status") == PENDING_STATUS:
        return "waiting for research runner"
    if job.get("status") == RUNNING_STATUS:
        return "running now"
    return "no terminal result yet"


def _agent_jobs_payload(
    jobs_dir: Path | None,
    *,
    limit: int,
    gateway_http_mounted: bool,
) -> dict:
    rows = list_jobs(jobs_dir, limit=limit) if jobs_dir is not None else []
    status_counts = Counter(str(job.get("status") or "UNKNOWN") for job in rows)
    pending = status_counts.get(PENDING_STATUS, 0)
    running = status_counts.get(RUNNING_STATUS, 0)
    done = status_counts.get(DONE_STATUS, 0)
    blocked = status_counts.get(BLOCKED_STATUS, 0)
    failed = status_counts.get(FAILED_STATUS, 0)
    recent: list[dict] = []
    for job in rows:
        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        recent.append(
            {
                "job_id": job.get("job_id"),
                "status": job.get("status"),
                "adapter": _agent_job_adapter(job),
                "created_by": job.get("created_by"),
                "hypothesis_id": request.get("hypothesis_id"),
                "strategy_id": request.get("strategy_id"),
                "exchange": request.get("exchange"),
                "symbol": request.get("symbol"),
                "timeframe": request.get("timeframe"),
                "updated_at": job.get("updated_at") or job.get("created_at"),
                "result_summary": _agent_job_result_summary(job),
                "can_trade": False,
                "can_promote": False,
                "live_orders_enabled": False,
            }
        )
    return {
        "summary": {
            "total": len(rows),
            "pending": pending,
            "running": running,
            "done": done,
            "blocked": blocked,
            "failed": failed,
            "terminal": sum(status_counts.get(status, 0) for status in TERMINAL_STATUSES),
            "gateway_http_mounted": gateway_http_mounted,
        },
        "jobs": recent,
        "jobs_dir": str(jobs_dir) if jobs_dir is not None else None,
        "policy": "dashboard-read-only; agent jobs cannot trade or promote",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _tail_lines(path: Path, max_bytes: int = 512_000) -> list[str]:
    """Bounded tail read: journals grow unbounded; never load them whole."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # first line is almost certainly partial
    return [line for line in lines if line.strip()]


def _iter_jsonl(path: Path, max_bytes: int = 512_000):
    for line in _tail_lines(path, max_bytes=max_bytes):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _summarize_payload(payload: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in list(payload.items())[:6])


def _alert_incidents(paths: list[Path]) -> list[dict]:
    out: list[dict] = []
    for path in paths:
        if not path.exists():
            continue
        for record in _iter_jsonl(path):
            rule_id = str(record.get("rule_id", ""))
            if rule_id in _NON_INCIDENT_ALERTS:
                continue
            anchor = _ALERT_RUNBOOKS.get(rule_id, _GENERAL_RUNBOOK)
            out.append({
                "ts": str(record.get("ts", "")),
                "severity": str(record.get("severity", "info")),
                "source": f"alert:{rule_id or 'unknown'}",
                "message": str(record.get("message", "")),
                "runbook": f"/runbooks#{anchor}",
            })
    return out


def _journal_incidents(journal_dir: Path | None) -> list[dict]:
    out: list[dict] = []
    if journal_dir is None or not journal_dir.is_dir():
        return out
    for path in sorted(journal_dir.glob("*.journal.jsonl")):
        lane = path.name.removesuffix(".journal.jsonl")
        for record in _iter_jsonl(path):
            kind = str(record.get("kind", ""))
            mapped = _INCIDENT_JOURNAL_KINDS.get(kind)
            if mapped is None:
                continue
            severity, anchor = mapped
            payload = record.get("payload")
            summary = _summarize_payload(payload) if isinstance(payload, dict) else ""
            out.append({
                "ts": str(record.get("ts", "")),
                "severity": severity,
                "source": f"journal:{lane}",
                "message": kind + (f" — {summary}" if summary else ""),
                "runbook": f"/runbooks#{anchor}",
            })
    return out


def _snapshot_trade_log(snapshot: dict | None, lane: str) -> list[dict]:
    """The trade log lives in the coalesced snapshot (multi-lane snapshots
    carry a per-lane tail; the primary lane's session carries the full one)."""
    if not isinstance(snapshot, dict):
        return []
    if lane:
        for entry in snapshot.get("lanes") or []:
            if isinstance(entry, dict) and entry.get("lane_id") == lane:
                return [e for e in entry.get("trade_log") or [] if isinstance(e, dict)]
        if snapshot.get("lane_id") != lane:
            return []
    session = snapshot.get("session")
    log = session.get("trade_log") if isinstance(session, dict) else None
    return [e for e in log or [] if isinstance(e, dict)]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _render_runbooks_html(markdown: str) -> str:
    """Minimal, dependency-free markdown: headings become anchored <h1..h3>,
    everything else is escaped verbatim inside <pre> blocks."""
    parts: list[str] = [
        "<!doctype html><meta charset='utf-8'><title>VNEDGE runbooks</title>",
        "<style>body{background:#05070a;color:#e8eef6;font:14px/1.55 ui-monospace,"
        "SFMono-Regular,Menlo,Consolas,monospace;max-width:860px;margin:24px auto;"
        "padding:0 16px}h1,h2,h3{color:#4cb7ff;scroll-margin-top:12px}"
        "h2{border-top:1px solid #263241;padding-top:18px}"
        "pre{white-space:pre-wrap;margin:4px 0}:target{color:#f7bd54}</style>",
    ]
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            parts.append("<pre>" + html.escape("\n".join(buffer)) + "</pre>")
            buffer.clear()

    for line in markdown.splitlines():
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            parts.append(
                f"<h{level} id='{_slug(title)}'>{html.escape(title)}</h{level}>"
            )
        else:
            buffer.append(line)
    flush()
    return "".join(parts)


def _cost_model_payload() -> dict:
    """The REAL round-trip cost model, read from the same constants the
    research and paper engines use — never hardcoded in the UI.

    Two honest cost models the operator must reconcile:
    - maker-first: maker entry + taker exit + slippage (the ~8 bps wall the
      scalper replay diagnostics use as breakeven).
    - taker round-trip: both legs taker + slippage (the ~11 bps wall).
    The paper broker's pessimistic fill model is reported alongside so the
    "8 vs 10 bps" disconnect is visible instead of buried in one number.
    """
    from vnedge.paper.fill_model import FillModel
    from vnedge.scalping.parameter_registry import (
        DEFAULT_SCALPER_PARAMETER_REGISTRY as _registry,
    )

    fee = _registry.fee_profile("binanceusdm")
    paper = FillModel()
    maker_first_rt = fee.maker_bps + fee.taker_bps + fee.slippage_bps
    taker_rt = 2 * fee.taker_bps + fee.slippage_bps
    paper_taker_rt = 2 * (paper.taker_fee_bps + paper.slippage_bps)

    # Every venue's real fee schedule, so the leverage/PnL calculator can model
    # each exchange from the SAME constants the research and paper engines use —
    # never a number hardcoded in the UI.
    exchanges = []
    for name, prof in sorted(_registry.exchange_fees.items()):
        exchanges.append({
            "exchange": prof.exchange,
            "label": _EXCHANGE_LABELS.get(prof.exchange, prof.exchange),
            "maker_bps": prof.maker_bps,
            "taker_bps": prof.taker_bps,
            "slippage_bps": prof.slippage_bps,
            "safety_buffer_bps": prof.safety_buffer_bps,
            "maker_first_cost_bps": round(prof.maker_first_cost_bps, 2),
            "taker_round_trip_cost_bps": round(prof.taker_round_trip_cost_bps, 2),
        })
    return {
        "exchange": fee.exchange,
        "source": "scalper_replay_diagnostics + paper.fill_model constants",
        "maker_bps": fee.maker_bps,
        "taker_bps": fee.taker_bps,
        "slippage_bps": fee.slippage_bps,
        "safety_buffer_bps": fee.safety_buffer_bps,
        # Two labelled round-trip cost models (no safety buffer — the raw wall).
        "maker_first_rt_bps": round(maker_first_rt, 2),
        "taker_rt_bps": round(taker_rt, 2),
        # With the research safety buffer applied (what the gates actually use).
        "maker_first_cost_bps": round(fee.maker_first_cost_bps, 2),
        "taker_round_trip_cost_bps": round(fee.taker_round_trip_cost_bps, 2),
        # Per-exchange schedules for the calculator (Binance / Bybit / Delta).
        "exchanges": exchanges,
        "paper_fill_model": {
            "taker_fee_bps": paper.taker_fee_bps,
            "slippage_bps": paper.slippage_bps,
            "taker_rt_bps": round(paper_taker_rt, 2),
        },
    }


class SnapshotProvider:
    """Holds the latest coalesced snapshot. The bot publishes; the UI reads.
    That is the entire coupling between them."""

    def __init__(self) -> None:
        self._latest: dict | None = None

    def publish(self, snapshot: dict) -> None:
        self._latest = snapshot

    def latest(self) -> dict | None:
        return self._latest


def create_app(
    provider: SnapshotProvider,
    token: str | None = None,
    snapshot_hz: float = 1.0,
    history_path: Path | None = None,
    research_path: Path | None = None,
    alpha_council_path: Path | None = None,
    alpha_workbench_path: Path | None = None,
    vibe_intelligence_path: Path | None = None,
    agentic_research_os_path: Path | None = None,
    alerts_path: Path | None = None,
    journal_dir: Path | None = None,
    runbooks_path: Path | None = None,
    lane_readiness_path: Path | None = None,
    realtime_scanner_path: Path | None = None,
    lane_firing_causality_path: Path | None = None,
    paper_lane_activation_path: Path | None = None,
    paper_route_doctor_path: Path | None = None,
    paper_lane_cadence_path: Path | None = None,
    paper_lane_performance_path: Path | None = None,
    paper_trade_entry_autopsy_path: Path | None = None,
    paper_trade_exit_autopsy_path: Path | None = None,
    paper_lane_root_cause_path: Path | None = None,
    maker_quote_lifecycle_path: Path | None = None,
    paper_trade_contract_reconciler_path: Path | None = None,
    lane_survival_path: Path | None = None,
    paper_lane_governor_path: Path | None = None,
    paper_roster_drift_path: Path | None = None,
    darwinian_agent_survival_path: Path | None = None,
    ml_pipeline_status_path: Path | None = None,
    pine_research_path: Path | None = None,
    quantified_strategy_lab_path: Path | None = None,
    quantified_port_factory_path: Path | None = None,
    quantified_pullback_proof_path: Path | None = None,
    pine_alpha_distiller_path: Path | None = None,
    backtest_progress_path: Path | None = None,
    pine_edge_uplift_path: Path | None = None,
    edge_uplift_executor_path: Path | None = None,
    scanner_backtest_uplift_path: Path | None = None,
    alpha_arena_lite_path: Path | None = None,
    quant_loop_governance_path: Path | None = None,
    evidence_index_path: Path | None = None,
    execution_replay_profile_path: Path | None = None,
    token_store: TokenStore | None = None,
    agent_token_store: AgentTokenStore | None = None,
    agent_audit_path: Path | None = None,
    agent_jobs_dir: Path | None = None,
    quant_os_agent_gateway_dir: Path | None = None,
) -> FastAPI:
    """Build the read-only dashboard app.

    Auth accepts either a per-user ``token_store`` (DASHBOARD_USERS), the
    legacy shared ``token`` (DASHBOARD_TOKEN — becomes the ``operator``
    user with no expiry), or both. Zero users refuses to start.
    """
    users: list[DashboardUser] = list(token_store.users) if token_store is not None else []
    if token is not None and token.strip():
        users.append(DashboardUser(name="operator", token=token.strip(), role="operator"))
    if not users:
        raise ValueError(
            "DASHBOARD_TOKEN or DASHBOARD_USERS must supply at least one user "
            "— no token, no dashboard"
        )
    store = TokenStore(users)

    app = FastAPI(title="VNEDGE dashboard", docs_url=None, redoc_url=None)
    ws_connections: dict[str, int] = {}  # user name -> live socket count (never tokens)

    @app.get("/health")
    async def health() -> JSONResponse:
        """Unauthenticated liveness probe for container healthchecks + the TLS
        proxy. Returns 200 as soon as the app is serving; deliberately requires
        NO token and reveals no state — its only job is "is the process up".
        This is what lets compose gate dependents on `service_healthy` and stop
        the --force-recreate race that took the fleet down twice."""
        return JSONResponse({"status": "ok"})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Unauthenticated READINESS probe — liveness says "process up", this
        says "up AND has data to serve". 200 once a snapshot has been published,
        503 while still warming. Reveals only that boolean, never any state, so
        it needs no token. Distinct from /health so an orchestrator can wait for
        data readiness before routing traffic without treating a warming
        process as dead."""
        if provider.latest() is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        return JSONResponse({"status": "ready"})

    # Per-lane files (equity/fills/journals/alerts) live next to the primary
    # equity history unless a journal dir is given explicitly.
    lane_dir = journal_dir or (history_path.parent if history_path is not None else None)
    # Resolve the runbooks doc across both layouts: dev (repo checkout, where
    # _REPO_ROOT/docs works) and the container (pip-installed package, where
    # __file__ points into site-packages but docs/ is COPYed to the WORKDIR).
    runbooks_file = runbooks_path or next(
        (c for c in (_REPO_ROOT / "docs" / "RUNBOOKS.md",
                     Path.cwd() / "docs" / "RUNBOOKS.md") if c.exists()),
        _REPO_ROOT / "docs" / "RUNBOOKS.md",
    )

    agent_jobs_path = agent_jobs_dir or env_agent_jobs_dir()
    quant_os_gateway = QuantOSAgentGateway(
        quant_os_agent_gateway_dir or env_quant_os_agent_gateway_dir()
    )
    resolved_agent_store = (
        agent_token_store if agent_token_store is not None else AgentTokenStore.from_env()
    )
    agent_gateway_http_mounted = bool(len(resolved_agent_store))
    if len(resolved_agent_store):
        mount_agent_gateway(
            app,
            provider=provider,
            token_store=resolved_agent_store,
            audit_logger=AgentAuditLogger(agent_audit_path or env_agent_audit_path()),
            jobs_dir=agent_jobs_path,
            quant_os_gateway_dir=quant_os_gateway.root,
            artifacts=AgentGatewayArtifacts(
                research_path=research_path,
                alpha_council_path=alpha_council_path,
                alpha_workbench_path=alpha_workbench_path,
                vibe_intelligence_path=vibe_intelligence_path,
                lane_readiness_path=lane_readiness_path,
                realtime_scanner_path=realtime_scanner_path,
            ),
        )

    def _authorized(request: Request) -> AuthResult:
        """Authenticate the request; raise 401 (with the store's reason —
        e.g. expiry) on failure. Never returns an unauthorized result."""
        header = request.headers.get("authorization", "")
        candidate = header.removeprefix("Bearer ").strip()
        if not candidate:
            candidate = request.query_params.get("token", "")
        result = store.authenticate(candidate)
        if not result.authorized:
            raise HTTPException(
                status_code=401, detail=result.reason or "missing or invalid token"
            )
        return result

    def _identity(user: AuthResult) -> dict[str, str]:
        # Role travels back with every authenticated response so a future
        # frontend can hide controls the caller can't use (defense in depth —
        # the server still enforces via _require_permission on control routes).
        return {"X-Dashboard-User": user.name or "", "X-Dashboard-Role": user.role or ""}

    def _require_permission(permission: str):
        """Dependency factory: authenticate, then 403 unless the caller's role
        grants ``permission``. Read routes need no gate today (every role has
        ``view``); this is the primitive that control routes (live-gate flip,
        promotion, kill-switch) attach to when they land — no second auth
        migration. Enforcement is server-side and cannot be spoofed by a header.
        """
        def _dep(request: Request) -> AuthResult:
            user = _authorized(request)
            if not has_permission(user.role, permission):
                raise HTTPException(
                    status_code=403,
                    detail=f"role {user.role!r} lacks permission {permission!r}",
                )
            return user
        return _dep

    def _read_json_payload(path: Path | None, fallback: dict) -> dict:
        if path is None or not path.exists():
            return fallback
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            return fallback  # mid-write race: serve a safe empty payload
        return payload if isinstance(payload, dict) else fallback

    pine_alpha_distiller_file = (
        pine_alpha_distiller_path
        or Path("research/live_research/pine_alpha_distiller_latest.json")
    )
    quantified_strategy_lab_file = (
        quantified_strategy_lab_path
        or Path("research/live_research/quantified_strategy_lab_latest.json")
    )
    quantified_port_factory_file = (
        quantified_port_factory_path
        or Path("research/live_research/quantified_port_factory_latest.json")
    )
    quantified_pullback_proof_file = (
        quantified_pullback_proof_path
        or Path("research/live_research/quantified_pullback_reversion_proof_latest.json")
    )
    pine_backtest_progress_file = (
        backtest_progress_path
        or Path("research/live_research/scanner_tournament_progress.json")
    )
    pine_edge_uplift_file = (
        pine_edge_uplift_path
        or Path("research/live_research/pine_edge_uplift_agent_latest.json")
    )
    edge_uplift_executor_file = (
        edge_uplift_executor_path
        or Path("research/live_research/edge_uplift_experiments_latest.json")
    )
    scanner_backtest_uplift_file = (
        scanner_backtest_uplift_path
        or Path("research/live_research/scanner_backtest_uplift_latest.json")
    )
    lane_firing_causality_file = (
        lane_firing_causality_path
        or Path("research/live_research/lane_firing_causality_latest.json")
    )
    alpha_arena_lite_file = (
        alpha_arena_lite_path
        or Path("research/live_research/alpha_arena_lite_latest.json")
    )
    quant_loop_governance_file = (
        quant_loop_governance_path
        or Path("research/live_research/quant_loop_governance_latest.json")
    )
    agentic_research_os_file = (
        agentic_research_os_path
        or Path("research/live_research/agentic_research_os_latest.json")
    )
    fee_wall_forensics_file = Path(
        "research/live_research/fee_wall_forensics_latest.json"
    )
    fee_wall_probes_file = Path(
        "research/live_research/fee_wall_paper_probes.json"
    )
    fee_wall_probe_actuals_file = Path(
        "research/live_research/fee_wall_probe_actuals_latest.json"
    )
    evidence_index_file = (
        evidence_index_path
        or Path("research/live_research/evidence_index_latest.json")
    )
    execution_replay_profile_file = (
        execution_replay_profile_path
        or Path("research/live_research/execution_replay_profile_latest.json")
    )
    paper_lane_activation_file = (
        paper_lane_activation_path
        or Path("research/live_research/paper_lane_activation_latest.json")
    )
    paper_route_doctor_file = (
        paper_route_doctor_path
        or Path("research/live_research/paper_route_doctor_latest.json")
    )
    paper_lane_cadence_file = (
        paper_lane_cadence_path
        or Path("research/live_research/paper_lane_cadence_latest.json")
    )
    paper_lane_performance_file = (
        paper_lane_performance_path
        or Path("research/live_research/paper_lane_performance_latest.json")
    )
    paper_trade_entry_autopsy_file = (
        paper_trade_entry_autopsy_path
        or Path("research/live_research/paper_trade_entry_autopsy_latest.json")
    )
    paper_trade_exit_autopsy_file = (
        paper_trade_exit_autopsy_path
        or Path("research/live_research/paper_trade_exit_autopsy_latest.json")
    )
    paper_lane_root_cause_file = (
        paper_lane_root_cause_path
        or Path("research/live_research/paper_lane_root_cause_latest.json")
    )
    maker_quote_lifecycle_file = (
        maker_quote_lifecycle_path
        or Path("research/live_research/maker_quote_lifecycle_latest.json")
    )
    paper_trade_contract_reconciler_file = (
        paper_trade_contract_reconciler_path
        or Path("research/live_research/paper_trade_contract_reconciler_latest.json")
    )
    lane_survival_file = (
        lane_survival_path
        or Path("research/live_research/lane_survival_latest.json")
    )
    paper_lane_governor_file = (
        paper_lane_governor_path
        or Path("research/live_research/paper_lane_governor_latest.json")
    )
    paper_roster_drift_file = (
        paper_roster_drift_path
        or Path("research/live_research/paper_roster_drift_latest.json")
    )
    darwinian_agent_survival_file = (
        darwinian_agent_survival_path
        or Path("research/live_research/darwinian_agent_survival_latest.json")
    )
    ml_pipeline_status_file = (
        ml_pipeline_status_path
        or Path("research/live_research/ml_pipeline_status.json")
    )

    @app.get("/")
    async def index() -> FileResponse:
        # The shell page contains no data; all data endpoints require the token.
        # no-store: the SPA ships on every deploy, so never let a browser serve a
        # stale cached shell — that showed empty panels against a live backend.
        return FileResponse(
            _STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/pine-research")
    async def pine_research_page() -> FileResponse:
        # Separate static research page. Data remains token-gated below.
        return FileResponse(
            _STATIC_DIR / "pine_research.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/quantified-strategy-lab")
    async def quantified_strategy_lab_page() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "quantified_strategy_lab.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/state")
    async def state(request: Request) -> JSONResponse:
        user = _authorized(request)
        snapshot = provider.latest()
        if snapshot is None:
            return JSONResponse(
                {"status": "no snapshot yet"}, status_code=503, headers=_identity(user)
            )
        return JSONResponse(snapshot, headers=_identity(user))

    @app.get("/whoami")
    async def whoami(request: Request) -> JSONResponse:
        """The authenticated caller's identity, role, and permission set.

        Read-only and reveals only the caller's OWN identity (never the token,
        never other users). A frontend uses `permissions` to show/hide controls;
        the server still enforces every control server-side via
        `_require_permission`."""
        user = _authorized(request)
        return JSONResponse(
            {
                "name": user.name,
                "role": user.role,
                "permissions": permissions_for(user.role),
                "expires_at": user.expires_at.isoformat() if user.expires_at else None,
            },
            headers=_identity(user),
        )

    def _query_lane(request: Request) -> str:
        lane = request.query_params.get("lane", "").strip()
        if lane and not _LANE_ID_RE.match(lane):
            raise HTTPException(status_code=400, detail="invalid lane id")
        return lane

    def _query_days(request: Request) -> float | None:
        raw = request.query_params.get("days", "").strip()
        if not raw:
            return None
        try:
            days = float(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="days must be a number")
        if days <= 0:
            raise HTTPException(status_code=400, detail="days must be positive")
        return days

    def _since_iso(days: float | None) -> str | None:
        if days is None:
            return None
        from datetime import UTC, datetime, timedelta

        return (datetime.now(UTC) - timedelta(days=days)).isoformat()

    def _lane_file(lane: str, suffix: str) -> Path | None:
        """Resolve a per-lane data file; empty lane means the primary lane."""
        if lane and lane_dir is not None:
            return lane_dir / f"{lane}{suffix}"
        if suffix == ".equity.jsonl":
            return history_path
        if history_path is not None and history_path.name.endswith(".equity.jsonl"):
            primary = history_path.name.removesuffix(".equity.jsonl")
            return history_path.parent / f"{primary}{suffix}"
        return None

    def _equity_points(lane: str, since: str | None) -> list[dict]:
        path = _lane_file(lane, ".equity.jsonl")
        points: list[dict] = []
        if path is not None and path.exists():
            for record in _iter_jsonl(path, max_bytes=4_000_000):
                if since is not None and str(record.get("ts", "")) < since:
                    continue
                points.append(record)
        return points[-2000:]

    @app.get("/history")
    async def history(request: Request) -> JSONResponse:
        """Persisted equity curve (survives restarts and page reloads).

        Optional filters: ?days=N (recent window) and ?lane=<id> (any lane's
        equity file next to the primary one)."""
        user = _authorized(request)
        lane = _query_lane(request)
        since = _since_iso(_query_days(request))
        return JSONResponse(_equity_points(lane, since), headers=_identity(user))

    @app.get("/export.csv")
    async def export_csv(request: Request) -> Response:
        """Per-lane CSV export: equity curve + trade log + fills, one flat
        table keyed by record_type. Same filters as /history."""
        user = _authorized(request)
        lane = _query_lane(request)
        since = _since_iso(_query_days(request))
        lane_label = lane
        if not lane_label and history_path is not None:
            lane_label = history_path.name.removesuffix(".equity.jsonl")
        lane_label = lane_label or "primary"

        fields = ["record_type", "ts", "lane", "equity", "event", "detail",
                  "symbol", "side", "quantity", "price", "fee_usd",
                  "realized_pnl_usd", "client_order_id"]

        def rows():
            for point in _equity_points(lane, since):
                yield {"record_type": "equity", "ts": point.get("ts", ""),
                       "equity": point.get("equity", "")}
            for event in _snapshot_trade_log(provider.latest(), lane):
                ts = str(event.get("ts", ""))
                if since is not None and ts < since:
                    continue
                yield {"record_type": "trade_log", "ts": ts,
                       "event": event.get("event", ""),
                       "detail": event.get("detail", "")}
            fills_path = _lane_file(lane, ".fills.jsonl")
            if fills_path is not None and fills_path.exists():
                for fill in _iter_jsonl(fills_path, max_bytes=4_000_000):
                    ts = str(fill.get("ts", ""))
                    if since is not None and ts < since:
                        continue
                    yield {"record_type": "fill", "ts": ts,
                           "symbol": fill.get("symbol", ""),
                           "side": fill.get("side", ""),
                           "quantity": fill.get("quantity", ""),
                           "price": fill.get("price", ""),
                           "fee_usd": fill.get("fee_usd", ""),
                           "realized_pnl_usd": fill.get("realized_pnl_usd", ""),
                           "client_order_id": fill.get("client_order_id", "")}

        def stream():
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows():
                writer.writerow({"lane": lane_label, **row})
                if buffer.tell() > 64_000:
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate()
            yield buffer.getvalue()

        return StreamingResponse(
            stream(),
            media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="vnedge_{lane_label}.csv"',
                     **_identity(user)},
        )

    @app.get("/trade-journal")
    async def trade_journal(request: Request, limit: str = "200") -> JSONResponse:
        """Read-only trade journal projection.

        Combines current snapshot positions/orders with per-lane decision
        journals and hash-chained fill ledgers. No controls, no mutations.
        """
        user = _authorized(request)
        lane = _query_lane(request)
        since = _since_iso(_query_days(request))
        try:
            limit = max(1, min(int(limit), 500))
        except ValueError:
            raise HTTPException(status_code=400, detail="limit must be an integer")
        return JSONResponse(
            build_trade_journal(
                snapshot=provider.latest(),
                journal_dir=lane_dir,
                history_path=history_path,
                lane=lane,
                since=since,
                limit=limit,
            ),
            headers=_identity(user),
        )

    @app.get("/incidents")
    async def incidents(request: Request) -> JSONResponse:
        """Merged reverse-chronological incident timeline: fired alerts plus
        incident-class decision-journal records, each with a runbook link."""
        user = _authorized(request)
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            raise HTTPException(status_code=400, detail="limit must be an integer")
        limit = max(1, min(limit, 500))
        alert_files: list[Path] = []
        if alerts_path is not None:
            alert_files.append(alerts_path)
        if lane_dir is not None and lane_dir.is_dir():
            alert_files.extend(
                p for p in sorted(lane_dir.glob("*.alerts.jsonl")) if p != alerts_path
            )
        merged = _alert_incidents(alert_files) + _journal_incidents(lane_dir)
        merged.sort(key=lambda record: record["ts"], reverse=True)
        return JSONResponse(merged[:limit], headers=_identity(user))

    @app.get("/runbooks")
    async def runbooks(request: Request) -> HTMLResponse:
        """docs/RUNBOOKS.md rendered minimally so incident links can anchor
        into it. Read-only, token-gated like every data route."""
        user = _authorized(request)
        try:
            markdown = runbooks_file.read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(status_code=404, detail="runbooks document not found")
        return HTMLResponse(_render_runbooks_html(markdown), headers=_identity(user))

    @app.get("/research")
    async def research(request: Request) -> JSONResponse:
        """Latest rolling walk-forward verdicts from the research loop."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(research_path, {"results": []}), headers=_identity(user)
        )

    @app.get("/cost-model")
    async def cost_model(request: Request) -> JSONResponse:
        """The real maker-first (~8bps) and taker (~11bps) round-trip cost
        models, read from the research/paper constants — not hardcoded in the
        UI. Auth-gated like every data route; read-only."""
        user = _authorized(request)
        return JSONResponse(_cost_model_payload(), headers=_identity(user))

    @app.get("/alpha-council")
    async def alpha_council(request: Request) -> JSONResponse:
        """Latest deterministic agent debate over research candidates."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                alpha_council_path,
                {"summary": {}, "debates": [], "can_trade": False, "can_promote": False},
            ),
            headers=_identity(user),
        )

    @app.get("/alpha-workbench")
    async def alpha_workbench(request: Request) -> JSONResponse:
        """Latest persistent proof-task backlog generated from the council."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                alpha_workbench_path,
                {"summary": {}, "tasks": [], "can_trade": False, "can_promote": False},
            ),
            headers=_identity(user),
        )

    @app.get("/vibe-intelligence")
    async def vibe_intelligence(request: Request) -> JSONResponse:
        """Latest persistent hypothesis lifecycle memory."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                vibe_intelligence_path,
                {"summary": {}, "cards": [], "can_trade": False, "can_promote": False},
            ),
            headers=_identity(user),
        )

    @app.get("/external-repo-synthesis")
    async def external_repo_synthesis(request: Request) -> JSONResponse:
        """Research-only synthesis of public repo review patterns.

        This is a source-attributed build queue, not a code import surface and
        not a trading/promotion route.
        """
        user = _authorized(request)
        return JSONResponse(build_external_repo_synthesis(), headers=_identity(user))

    @app.get("/agentic-research-os")
    async def agentic_research_os(request: Request) -> JSONResponse:
        """Latest Agentic Research OS supervisor report.

        This is dashboard-token gated and research-only. It ranks agent work,
        verifier gaps, stale tasks, and keep/decay/retire actions without
        granting trade or promotion authority.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                agentic_research_os_file,
                {
                    "os_id": "agentic_research_os_v2",
                    "summary": {},
                    "agent_scorecards": [],
                    "operator_queue": [],
                    "source_status": [],
                    "operator_answer": "agentic research os artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                    "live_orders_enabled": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/agent-jobs")
    async def agent_jobs(request: Request, limit: int = 100) -> JSONResponse:
        """Operator-facing Agent Gateway job ledger.

        This is dashboard-token gated and read-only. It works even when the
        agent HTTP API is intentionally unmounted because no agent tokens are
        configured.
        """
        user = _authorized(request)
        limit = max(1, min(int(limit), 200))
        return JSONResponse(
            _agent_jobs_payload(
                agent_jobs_path,
                limit=limit,
                gateway_http_mounted=agent_gateway_http_mounted,
            ),
            headers=_identity(user),
        )

    @app.get("/quant-os/agent-gateway")
    async def quant_os_agent_gateway(request: Request, limit: int = 100) -> JSONResponse:
        """Operator-facing Quant OS Agent Gateway v2 ledger.

        This is dashboard-token gated and read-only. Agent-token write routes
        live under /api/agent/v2 and still cannot trade or promote.
        """
        user = _authorized(request)
        return JSONResponse(
            quant_os_gateway.snapshot(limit=max(1, min(int(limit), 250))),
            headers=_identity(user),
        )

    @app.get("/quant-os/agent-gateway/events")
    async def quant_os_agent_gateway_events(request: Request, limit: int = 100) -> Response:
        """Recent Agent Gateway v2 events as JSON or finite SSE frames."""
        user = _authorized(request)
        snapshot = quant_os_gateway.snapshot(limit=max(1, min(int(limit), 250)))
        if "text/event-stream" in request.headers.get("accept", ""):
            return StreamingResponse(
                iter(quant_os_event_stream(snapshot)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", **_identity(user)},
            )
        return JSONResponse(
            {
                "gateway_id": snapshot["gateway_id"],
                "events": snapshot["events"],
                "can_trade": False,
                "can_promote": False,
            },
            headers=_identity(user),
        )

    @app.get("/lane-readiness")
    async def lane_readiness(request: Request) -> JSONResponse:
        """Latest lane firing/promotability report."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                lane_readiness_path,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "lane readiness report unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/ml-status")
    async def ml_status(request: Request) -> JSONResponse:
        """ML pipeline status — the meta-labeling training set accumulating from
        live journals, the pipeline stage, and the locked promotion gates.
        Read-only; no model trades outside the gateway/registry."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                ml_pipeline_status_file,
                {
                    "stage": "COLLECTING_LABELS",
                    "stages": [],
                    "dataset": {"samples": 0, "min_to_train": 200, "progress_pct": 0.0, "by_strategy": {}},
                    "foundation": {},
                    "gates": {},
                    "model": None,
                    "can_trade": False,
                    "can_promote": False,
                    "note": "ml pipeline status unavailable",
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pre-live-checklist")
    async def pre_live_checklist(request: Request) -> JSONResponse:
        """The gates to a first live order + who must act on each red (deliberate
        / operator / system) + the ordered path to live. Computed on demand,
        read-only; booleans only — it never reads a secret value and cannot
        enable live trading."""
        user = _authorized(request)
        from vnedge.research.pre_live_status import build_pre_live_status

        ladder = _REPO_ROOT / "research" / "live_research" / "live_ladder_latest.json"
        return JSONResponse(
            build_pre_live_status(
                journal_dir=lane_dir or Path("logs/paper_trials"),
                ladder_path=ladder if ladder.exists() else None,
            ),
            headers=_identity(user),
        )

    @app.get("/paper-lane-activation")
    async def paper_lane_activation(request: Request) -> JSONResponse:
        """Latest paper activation truth board.

        This reconciles paper manifests, runtime paper routes, scanner pressure,
        and paper journals. It is read-only and cannot start or promote a lane.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_lane_activation_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "paper lane activation report unavailable",
                    "mode": "read_only_activation_truth",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-lane-performance")
    async def paper_lane_performance(request: Request) -> JSONResponse:
        """Latest paper performance ledger.

        This summarizes paper journals and hash-chained fill ledgers into
        per-lane PnL/PF/sample status. It is read-only and cannot promote.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_lane_performance_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "paper performance report unavailable",
                    "mode": "read_only_paper_performance",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-trade-exit-autopsy")
    async def paper_trade_exit_autopsy(request: Request) -> JSONResponse:
        """Latest paper trade exit autopsy.

        This explains closed paper-trade loss drivers from fills + exit journal
        metadata. It is read-only and cannot promote, demote, or trade.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_trade_exit_autopsy_file,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "paper trade exit autopsy unavailable",
                    "mode": "read_only_paper_trade_exit_autopsy",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-trade-entry-autopsy")
    async def paper_trade_entry_autopsy(request: Request) -> JSONResponse:
        """Latest paper trade entry autopsy.

        This joins closed paper entries to prior fired lane_eval context so
        operators can see stale entries, missing signal linkage, direction
        drift, and fee-wall-short expected edge. It is read-only.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_trade_entry_autopsy_file,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "paper trade entry autopsy unavailable",
                    "mode": "read_only_paper_trade_entry_autopsy",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/maker-quote-lifecycle")
    async def maker_quote_lifecycle(request: Request) -> JSONResponse:
        """Latest maker quote lifecycle report.

        This explains whether a lane has actual post-only maker quote, fill,
        cancel, and fee-aware taker fallback proof. It is read-only and cannot
        trade, promote, demote, or restart routes.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                maker_quote_lifecycle_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "maker quote lifecycle report unavailable",
                    "mode": "read_only_maker_quote_lifecycle",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-trade-contract-reconciler")
    async def paper_trade_contract_reconciler(request: Request) -> JSONResponse:
        """Latest paper trade contract reconciliation.

        This distinguishes execution/journal contract drift from contract-clean
        alpha/exit failure. It is read-only and cannot promote, demote, or trade.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_trade_contract_reconciler_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "trade_samples": [],
                    "operator_answer": "paper trade contract reconciler unavailable",
                    "mode": "read_only_paper_contract_reconciliation",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/lane-survival")
    async def lane_survival(request: Request) -> JSONResponse:
        """Latest lane survival engine report.

        This reconciles activation, route, cadence, and corrected performance
        into keep/observe/demote/repair recommendations. It is read-only and
        cannot mutate routes or promote a lane.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                lane_survival_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "lane survival report unavailable",
                    "mode": "read_only_lane_survival",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-lane-governor")
    async def paper_lane_governor(request: Request) -> JSONResponse:
        """Latest paper lane governor report.

        This turns lane-survival evidence into a proposed paper roster,
        survivor tournament, demotion queue, and repair queue. It is read-only
        and cannot mutate runtime lanes.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_lane_governor_file,
                {
                    "summary": {},
                    "proposed_roster": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "paper lane governor report unavailable",
                    "mode": "read_only_paper_lane_governor",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/darwinian-agent-survival")
    async def darwinian_agent_survival(request: Request) -> JSONResponse:
        """Latest Atlas-inspired agent/cohort survival report.

        This computes advisory Darwinian weights and JANUS cohort weights from
        existing evidence. It is read-only and cannot mutate runtime lanes.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                darwinian_agent_survival_file,
                {
                    "summary": {},
                    "cohorts": [],
                    "agents": [],
                    "operator_answer": "darwinian agent survival report unavailable",
                    "mode": "atlas_inspired_read_only_agent_survival",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-roster-drift")
    async def paper_roster_drift(request: Request) -> JSONResponse:
        """Latest paper roster drift report.

        This compares the governor's proposed paper roster to runtime scanner
        and activation evidence, naming extra/missing paper lanes. It is
        read-only and cannot mutate runtime lanes.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_roster_drift_file,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "paper roster drift report unavailable",
                    "mode": "read_only_paper_roster_drift",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/paper-route-doctor")
    async def paper_route_doctor(request: Request) -> JSONResponse:
        """Latest paper route/journal doctor.

        It explains whether approved paper routes have fresh journal proof and
        whether the runner service is visible. Read-only; no restarts/trades.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_route_doctor_file,
                {
                    "summary": {},
                    "rows": [],
                    "runner_service": {"state": "unknown", "up": None},
                    "operator_answer": "paper route doctor report unavailable",
                    "mode": "read_only_paper_route_doctor",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/trade-profile-matrix")
    async def trade_profile_matrix(request: Request) -> JSONResponse:
        """Read-only paper/live sizing profile matrix.

        It is derived from the paper activation artifact. Dashboard inputs are
        planner-only; this endpoint cannot apply margin/leverage changes.
        """
        user = _authorized(request)
        from vnedge.research.trade_profile_matrix import build_trade_profile_matrix

        activation = _read_json_payload(
            paper_lane_activation_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper lane activation report unavailable",
                "mode": "read_only_activation_truth",
                "can_trade": False,
                "can_promote": False,
            },
        )
        return JSONResponse(
            build_trade_profile_matrix(activation),
            headers=_identity(user),
        )

    @app.get("/paper-lane-cadence")
    async def paper_lane_cadence(request: Request) -> JSONResponse:
        """Latest paper lane evaluation cadence report.

        It tells whether routed paper lanes are emitting live lane_eval events
        frequently enough for their timeframe. Read-only; no restarts/trades.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_lane_cadence_file,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "paper lane cadence report unavailable",
                    "mode": "read_only_paper_lane_cadence",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/operator-actions")
    async def operator_actions(request: Request) -> JSONResponse:
        """Read-only ranked action queue for paper/scanner operations.

        Joins activation, route doctor, cadence, profile, performance, and
        causality evidence into one operator answer. It cannot trade, promote,
        restart runners, or apply profile changes.
        """
        user = _authorized(request)
        from vnedge.research.operator_actions import build_operator_actions
        from vnedge.research.trade_profile_matrix import build_trade_profile_matrix

        activation = _read_json_payload(
            paper_lane_activation_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper lane activation report unavailable",
                "mode": "read_only_activation_truth",
                "can_trade": False,
                "can_promote": False,
            },
        )
        route = _read_json_payload(
            paper_route_doctor_file,
            {
                "summary": {},
                "rows": [],
                "runner_service": {"state": "unknown", "up": None},
                "operator_answer": "paper route doctor report unavailable",
                "mode": "read_only_paper_route_doctor",
                "can_trade": False,
                "can_promote": False,
            },
        )
        cadence = _read_json_payload(
            paper_lane_cadence_file,
            {
                "summary": {},
                "rows": [],
                "operator_answer": "paper lane cadence report unavailable",
                "mode": "read_only_paper_lane_cadence",
                "can_trade": False,
                "can_promote": False,
            },
        )
        performance = _read_json_payload(
            paper_lane_performance_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper performance report unavailable",
                "mode": "read_only_paper_performance",
                "can_trade": False,
                "can_promote": False,
            },
        )
        exit_autopsy = _read_json_payload(
            paper_trade_exit_autopsy_file,
            {
                "summary": {},
                "rows": [],
                "operator_answer": "paper trade exit autopsy unavailable",
                "mode": "read_only_paper_trade_exit_autopsy",
                "can_trade": False,
                "can_promote": False,
            },
        )
        contract_reconciler = _read_json_payload(
            paper_trade_contract_reconciler_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "trade_samples": [],
                "operator_answer": "paper trade contract reconciler unavailable",
                "mode": "read_only_paper_contract_reconciliation",
                "can_trade": False,
                "can_promote": False,
            },
        )
        causality = _read_json_payload(
            lane_firing_causality_file,
            {
                "summary": {},
                "promotion_board": {},
                "rows": [],
                "operator_answer": "lane firing causality report unavailable",
                "mode": "read_only_operator_truth",
                "can_trade": False,
                "can_promote": False,
            },
        )
        return JSONResponse(
            build_operator_actions(
                activation=activation,
                route=route,
                cadence=cadence,
                performance=performance,
                exit_autopsy=exit_autopsy,
                contract_reconciler=contract_reconciler,
                profile=build_trade_profile_matrix(activation),
                causality=causality,
            ),
            headers=_identity(user),
        )

    @app.get("/paper-lane-root-cause")
    async def paper_lane_root_cause(request: Request) -> JSONResponse:
        """Read-only root-cause matrix for paper lanes.

        Joins activation, route, cadence, sizing profile, entry/exit autopsy,
        performance, survival, governor, and causality into one primary
        blocker per lane. It cannot trade, promote, demote, or apply fixes.
        """
        user = _authorized(request)
        cached = _read_json_payload(
            paper_lane_root_cause_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper lane root-cause report unavailable",
                "mode": "read_only_paper_lane_root_cause",
                "can_trade": False,
                "can_promote": False,
            },
        )
        if cached.get("rows") or cached.get("summary"):
            return JSONResponse(cached, headers=_identity(user))

        from vnedge.research.paper_lane_root_cause import build_paper_lane_root_cause
        from vnedge.research.trade_profile_matrix import build_trade_profile_matrix

        activation = _read_json_payload(
            paper_lane_activation_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper lane activation report unavailable",
                "mode": "read_only_activation_truth",
                "can_trade": False,
                "can_promote": False,
            },
        )
        route = _read_json_payload(
            paper_route_doctor_file,
            {
                "summary": {},
                "rows": [],
                "runner_service": {"state": "unknown", "up": None},
                "operator_answer": "paper route doctor report unavailable",
                "mode": "read_only_paper_route_doctor",
                "can_trade": False,
                "can_promote": False,
            },
        )
        cadence = _read_json_payload(
            paper_lane_cadence_file,
            {
                "summary": {},
                "rows": [],
                "operator_answer": "paper lane cadence report unavailable",
                "mode": "read_only_paper_lane_cadence",
                "can_trade": False,
                "can_promote": False,
            },
        )
        performance = _read_json_payload(
            paper_lane_performance_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper performance report unavailable",
                "mode": "read_only_paper_performance",
                "can_trade": False,
                "can_promote": False,
            },
        )
        exit_autopsy = _read_json_payload(
            paper_trade_exit_autopsy_file,
            {
                "summary": {},
                "rows": [],
                "operator_answer": "paper trade exit autopsy unavailable",
                "mode": "read_only_paper_trade_exit_autopsy",
                "can_trade": False,
                "can_promote": False,
            },
        )
        survival = _read_json_payload(
            lane_survival_file,
            {
                "summary": {},
                "boards": {},
                "rows": [],
                "operator_answer": "lane survival report unavailable",
                "mode": "read_only_lane_survival",
                "can_trade": False,
                "can_promote": False,
            },
        )
        governor = _read_json_payload(
            paper_lane_governor_file,
            {
                "summary": {},
                "proposed_roster": {},
                "boards": {},
                "rows": [],
                "operator_answer": "paper lane governor report unavailable",
                "mode": "read_only_paper_lane_governor",
                "can_trade": False,
                "can_promote": False,
            },
        )
        causality = _read_json_payload(
            lane_firing_causality_file,
            {
                "summary": {},
                "promotion_board": {},
                "rows": [],
                "operator_answer": "lane firing causality report unavailable",
                "mode": "read_only_operator_truth",
                "can_trade": False,
                "can_promote": False,
            },
        )
        return JSONResponse(
            build_paper_lane_root_cause(
                activation=activation,
                route=route,
                cadence=cadence,
                performance=performance,
                exit_autopsy=exit_autopsy,
                survival=survival,
                governor=governor,
                profile=build_trade_profile_matrix(activation),
                causality=causality,
            ),
            headers=_identity(user),
        )

    fleet_status_file = Path("logs/fleet.json")

    @app.get("/meta")
    async def meta(request: Request) -> JSONResponse:
        """Build provenance: deployed git sha (baked at image build), host, and
        dashboard-process uptime. Read-only."""
        _authorized(request)
        return JSONResponse(
            {
                "build_sha": _build_sha(),
                "host": os.environ.get("VNEDGE_HOST") or socket.gethostname(),
                "uptime_seconds": int(max(0.0, time.time() - _APP_START)),
            }
        )

    @app.get("/fleet")
    async def fleet(request: Request) -> JSONResponse:
        """Container fleet status, written host-side by scripts/fleet_status.sh
        (the dashboard container has no docker access). Empty until the host
        timer runs. Read-only."""
        _authorized(request)
        payload = _read_json_payload(fleet_status_file, {"services": [], "written_at": None})
        return JSONResponse(payload)

    @app.get("/scorecard")
    async def scorecard(request: Request) -> JSONResponse:
        """Per-strategy scanner scorecard: best net edge (bps), fee-wall verdict,
        profit factor and break rate from the fee-wall forensics artifact, plus
        the approved paper-probe promotion queue. Read-only research surface —
        cannot trade or promote."""
        _authorized(request)
        forensics = _read_json_payload(fee_wall_forensics_file, {"reports": []})
        probes = _read_json_payload(fee_wall_probes_file, {"paper_probes": []})
        probe_actuals = _read_json_payload(
            fee_wall_probe_actuals_file, {"rows": [], "summary": {}}
        )
        by: dict = {}
        for r in forensics.get("reports", []):
            strat = r.get("strategy")
            summ = r.get("summary") or {}
            net = summ.get("avg_selected_net_bps")
            if not strat or net is None:
                continue
            g = by.setdefault(
                strat,
                {
                    "strategy": strat, "best_net_bps": None, "verdict": None,
                    "profit_factor": None, "break_rate_pct": None,
                    "samples": 0, "venues": set(),
                },
            )
            if r.get("exchange"):
                g["venues"].add(r["exchange"])
            g["samples"] += int(summ.get("opportunities") or 0)
            if g["best_net_bps"] is None or net > g["best_net_bps"]:
                g["best_net_bps"] = net
                g["verdict"] = summ.get("verdict")
                g["profit_factor"] = summ.get("profit_factor")
                g["break_rate_pct"] = summ.get("fee_wall_break_rate_pct")
        rows = []
        for g in by.values():
            g = dict(g)
            g["venues"] = sorted(v for v in g["venues"] if v)
            rows.append(g)
        rows.sort(key=lambda r: (r["best_net_bps"] is None, -(r["best_net_bps"] or -1e9)))
        return JSONResponse(
            {
                "generated_at": forensics.get("generated_at"),
                "strategies": rows,
                "probes": probes.get("paper_probes", []),
                "probe_actuals": probe_actuals.get("rows", []),
                "probe_actuals_summary": probe_actuals.get("summary", {}),
                "can_trade": False,
                "can_promote": False,
            }
        )

    @app.get("/realtime-scanner")
    async def realtime_scanner(request: Request) -> JSONResponse:
        """Latest live scanner pressure report.

        This is intentionally separate from replay/candidate-replay reports:
        it reads current runtime journals only and cannot trade or promote.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                realtime_scanner_path,
                {
                    "summary": {},
                    "rows": [],
                    "operator_answer": "real-time scanner report unavailable",
                    "mode": "live_observation_not_replay",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/lane-firing-causality")
    async def lane_firing_causality(request: Request) -> JSONResponse:
        """Joined lane truth: live scanner cause, risk/execution route, and
        paper promotion state. Read-only and non-promoting."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                lane_firing_causality_file,
                {
                    "summary": {},
                    "promotion_board": {},
                    "rows": [],
                    "operator_answer": "lane firing causality report unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/kb")
    async def pine_research_kb(request: Request) -> JSONResponse:
        """Public-script review KB.

        Read-only, dashboard-token gated, and explicitly research-only. This
        endpoint can be backed by a generated artifact once the crawler/review
        pipeline publishes it; until then it serves a conservative seed.
        """
        user = _authorized(request)
        return JSONResponse(
            load_pine_research_payload(pine_research_path),
            headers=_identity(user),
        )

    @app.get("/quantified-strategy-lab/kb")
    async def quantified_strategy_lab_kb(request: Request) -> JSONResponse:
        """Title-only 95-strategy inventory triage.

        This endpoint deliberately carries no executable strategy rules. It
        groups the 95 titles into VNEDGE-owned research hypotheses and replay
        queues while preserving the no-copy/no-promotion boundary.
        """
        user = _authorized(request)
        return JSONResponse(
            load_quantified_strategy_lab_payload(quantified_strategy_lab_file),
            headers=_identity(user),
        )

    @app.get("/quantified-strategy-lab/port-factory")
    async def quantified_strategy_lab_port_factory(request: Request) -> JSONResponse:
        """Agent-ready VNEDGE port tasks derived from the title inventory."""
        user = _authorized(request)
        return JSONResponse(
            load_quantified_port_factory_payload(quantified_port_factory_file),
            headers=_identity(user),
        )

    @app.get("/quantified-strategy-lab/pullback-proof")
    async def quantified_strategy_lab_pullback_proof(request: Request) -> JSONResponse:
        """Research-only proof queue for the first Quantified pullback port."""
        user = _authorized(request)
        return JSONResponse(
            load_quantified_pullback_reversion_proof_payload(
                quantified_pullback_proof_file
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/distiller")
    async def pine_alpha_distiller(request: Request) -> JSONResponse:
        """Source-backed Pine primitive/task distillation artifact."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                pine_alpha_distiller_file,
                {
                    "distiller_id": "pine_alpha_distiller_v1",
                    "summary": {},
                    "primitive_families": [],
                    "port_tasks": [],
                    "script_distillations": [],
                    "operator_answer": "pine alpha distiller artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/progress")
    async def pine_backtest_progress(request: Request) -> JSONResponse:
        """Live scanner tournament/backtest progress.

        This is operational visibility only: it reports the in-flight research
        worker heartbeat and never grants trade or promotion permission.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                pine_backtest_progress_file,
                {
                    "truth_layer": "scanner_tournament_progress_v1",
                    "status": "idle",
                    "phase": "no_progress_artifact",
                    "started_at": None,
                    "heartbeat_at": None,
                    "completed_at": None,
                    "profile": None,
                    "lookback_days": None,
                    "target_count": 0,
                    "strategy_count": 0,
                    "total_work_units": 0,
                    "completed_work_units": 0,
                    "progress_pct": 0.0,
                    "current_target": None,
                    "current_strategy": None,
                    "current_rows": None,
                    "current_routes": None,
                    "output_path": None,
                    "last_error": None,
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/uplift-agent")
    async def pine_edge_uplift_agent(request: Request) -> JSONResponse:
        """Agentic failure-salvage and edge-uplift artifact."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                pine_edge_uplift_file,
                {
                    "agent_id": "pine_edge_uplift_agent_v1",
                    "summary": {},
                    "failure_clusters": [],
                    "top_uplifts": [],
                    "experiments": [],
                    "operator_answer": "pine edge uplift agent artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/uplift-executor")
    async def edge_uplift_executor(request: Request) -> JSONResponse:
        """Replay/port task queue produced from the edge-uplift agent."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                edge_uplift_executor_file,
                {
                    "executor_id": "edge_uplift_executor_v1",
                    "summary": {},
                    "port_pack": [],
                    "tasks": [],
                    "operator_answer": "edge uplift executor artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/scanner-uplift")
    async def scanner_backtest_uplift(request: Request) -> JSONResponse:
        """Backtest-failure classifications and scanner uplift experiments."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                scanner_backtest_uplift_file,
                {
                    "agent_id": "scanner_backtest_uplift_v1",
                    "summary": {},
                    "top_uplifts": [],
                    "experiments": [],
                    "operator_answer": "scanner backtest uplift artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/alpha-arena-lite")
    async def alpha_arena_lite(request: Request) -> JSONResponse:
        """Durable Arena task/scorecard layer for scanner uplift candidates."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                alpha_arena_lite_file,
                {
                    "arena_id": "alpha_arena_lite_v1",
                    "summary": {},
                    "scorecards": [],
                    "gateway": {},
                    "operator_answer": "alpha arena lite artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/quant-loop-governance")
    async def quant_loop_governance(request: Request) -> JSONResponse:
        """Research-loop readiness, collision, and budget governance."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                quant_loop_governance_file,
                {
                    "governance_id": "quant_loop_governance_v1",
                    "summary": {},
                    "gate_checks": [],
                    "loop_cards": [],
                    "candidate_locks": [],
                    "collisions": [],
                    "budget_alerts": [],
                    "operator_answer": "quant loop governance artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                    "live_orders_enabled": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/evidence-index")
    async def pine_research_evidence_index(request: Request) -> JSONResponse:
        """Unified research evidence index across Pine/scanner/arena artifacts."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                evidence_index_file,
                {
                    "evidence_store_id": "research_evidence_index_v1",
                    "summary": {},
                    "records": [],
                    "top_positive": [],
                    "fee_wall_breakers": [],
                    "sparse_positives": [],
                    "failure_clusters": [],
                    "operator_answer": "research evidence index artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                    "live_orders_enabled": False,
                },
            ),
            headers=_identity(user),
        )

    @app.get("/pine-research/execution-profile")
    async def pine_research_execution_profile(request: Request) -> JSONResponse:
        """Execution-realistic replay profile for research evidence rows."""
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                execution_replay_profile_file,
                {
                    "execution_profile_id": "execution_realistic_replay_profile_v1",
                    "summary": {},
                    "profiles": [],
                    "settlement_logic_evaluation": {"components": []},
                    "rows": [],
                    "execution_ready_rows": [],
                    "paper_blocked_rows": [],
                    "operator_answer": "execution replay profile artifact unavailable",
                    "can_trade": False,
                    "can_promote": False,
                    "live_orders_enabled": False,
                },
            ),
            headers=_identity(user),
        )

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        result = store.authenticate(websocket.query_params.get("token", ""))
        if not result.authorized:
            await websocket.close(
                code=4401, reason=(result.reason or "missing or invalid token")[:120]
            )
            return
        name = result.name or "?"
        await websocket.accept()
        ws_connections[name] = ws_connections.get(name, 0) + 1
        logger.info("dashboard ws connected: user=%s role=%s", name, result.role)
        try:
            while True:
                if result.expires_at is not None and (
                    datetime.now(timezone.utc) >= result.expires_at
                ):
                    # A token that expires mid-session loses the stream too.
                    await websocket.close(code=4401, reason="token expired")
                    return
                snapshot = provider.latest()
                if snapshot is not None:
                    await websocket.send_json(
                        # Who's connected: count only — names and tokens are
                        # never serialized into the snapshot.
                        {**snapshot, "dashboard_connections": sum(ws_connections.values())}
                    )
                await asyncio.sleep(1.0 / snapshot_hz)
        except (WebSocketDisconnect, ConnectionError):
            return  # dropped client: deregistered by scope exit, bot unaffected
        except Exception as exc:  # noqa: BLE001 — UI must never propagate upward
            logger.warning("dashboard websocket dropped: %s", exc)
            return
        finally:
            remaining = ws_connections.get(name, 1) - 1
            if remaining <= 0:
                ws_connections.pop(name, None)
            else:
                ws_connections[name] = remaining
            logger.info("dashboard ws disconnected: user=%s", name)

    return app
