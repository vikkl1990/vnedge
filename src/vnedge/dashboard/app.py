"""Safety-first dashboard server (docs/DESIGN.md §6).

Hard invariants, enforced structurally:
- No token, no dashboard: `create_app` refuses to start without at least one
  authorized user (legacy shared token or per-user store — see auth.py and
  docs/DASHBOARD_AUTH.md).
- Zero order or promotion actions: scoped mutations can manage operator
  settings or queue a bounded research-only backtest, but cannot mutate
  trading state, execute research inline, or promote a strategy.
- Cannot slow the bot: the server only reads whatever snapshot the bot last
  published; a dead or slow browser drops its own socket and nothing else.
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import html
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.middleware.base import RequestResponseEndpoint

from vnedge.agent_gateway.app import (
    AgentGatewayArtifacts,
    BacktestRequest,
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
    create_backtest_job,
    list_jobs,
)
from vnedge.agent_gateway.task_registry import (
    QuantOSAgentGateway,
    env_quant_os_agent_gateway_dir,
    quant_os_event_stream,
)
from vnedge.dashboard.auth import (
    PERM_MANAGE_SETTINGS,
    PERM_REQUEST_BACKTEST,
    AuthResult,
    DashboardUser,
    TokenStore,
    has_permission,
    permissions_for,
)
from vnedge.dashboard.backtest_lab import load_backtest_lab
from vnedge.dashboard.chart_series import candles_payload, mechanism_context_payload
from vnedge.dashboard.correction_ui import build_lanes_payload, build_risk_payload
from vnedge.dashboard.market_pulse import MarketPulseService
from vnedge.dashboard.session import SessionIssuer
from vnedge.dashboard.session_regime import build_session_regime
from vnedge.dashboard.trade_journal import build_trade_journal
from vnedge.data.candles import CandleParquetStore
from vnedge.execution.operator_audit import OperatorAuditLog
from vnedge.research.external_repo_synthesis import build_external_repo_synthesis
from vnedge.research.performance_scorecard import performance_disclosure, scorecard_policy
from vnedge.research.quantified_port_factory import load_quantified_port_factory_payload
from vnedge.research.quantified_strategy_lab import load_quantified_strategy_lab_payload
from vnedge.research.scanner_catalog import live_catalog
from vnedge.research.strategy_workflow import build_strategy_workflow
from vnedge.settings.api_routes import mount_settings_routes
from vnedge.settings.crypto import SecretBox
from vnedge.settings.exchange_connections import SettingsService
from vnedge.settings.store import SettingsStore


def _load_retired_artifact(path: Path | None, fallback: dict | None = None) -> dict:
    """Read an old research artifact without importing its retired generator."""
    if path is None or not path.exists():
        return dict(fallback or {})
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return dict(fallback or {})
    return payload if isinstance(payload, dict) else dict(fallback or {})


def load_pine_research_payload(path: Path | None) -> dict:
    return _load_retired_artifact(path, {"retired": True, "rows": []})


load_quantified_blueprint_proof_payload = _load_retired_artifact
load_quantified_proof_result_arbiter_payload = _load_retired_artifact
load_quantified_pullback_reversion_proof_payload = _load_retired_artifact

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_START = time.time()
_SESSION_COOKIE = "vnedge_session"
_CSRF_COOKIE = "vnedge_csrf"


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
    if not math.isfinite(parsed):
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


def _snapshot_health_incidents(snapshot: dict | None) -> list[dict]:
    """Project current lane failures into the incident rail without persistence."""
    if not snapshot:
        return []
    generated = datetime.now(UTC).isoformat()
    out: list[dict] = []
    for lane in build_lanes_payload(snapshot, now=datetime.now(UTC)).get("lanes", []):
        health = str(lane.get("health") or "unknown")
        if health not in {"blocked", "degraded"}:
            continue
        lane_id = str(lane.get("lane_id") or "unknown")
        reason = str(lane.get("health_reason") or lane.get("current_waiting_reason") or health)
        out.append(
            {
                "ts": generated,
                "severity": "critical" if health == "blocked" else "warning",
                "source": f"runtime:{lane_id}",
                "message": f"lane_{health} — {reason}",
                "runbook": "/runbooks#feed-stale",
            }
        )
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
        (
            "<style>body{background:#05070a;color:#e8eef6;"
            "font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
            "max-width:860px;margin:24px auto;padding:0 16px}"
            "h1,h2,h3{color:#4cb7ff;scroll-margin-top:12px}"
            "h2{border-top:1px solid #263241;padding-top:18px}"
            "pre{white-space:pre-wrap;margin:4px 0}"
            ":target{color:#f7bd54}</style>"
        ),
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
    """Canonical active fee wall plus explicitly labelled evidence schedules."""
    from vnedge.paper.fill_model import FillModel
    from vnedge.plan.cost_model import CostModel
    from vnedge.scalping.parameter_registry import (
        DEFAULT_SCALPER_PARAMETER_REGISTRY as _registry,
    )

    canonical = CostModel.for_profile("scalp")
    config = canonical.config
    fee = _registry.fee_profile("binanceusdm")
    paper = FillModel()
    maker_first_rt = canonical.round_trip_bps(maker_entry=True, include_safety=False)
    taker_rt = canonical.round_trip_bps(include_safety=False)
    paper_taker_rt = 2 * (paper.taker_fee_bps + paper.slippage_bps)

    # Every venue's real fee schedule, so the leverage/PnL calculator can model
    # each exchange from the SAME constants the research and paper engines use —
    # never a number hardcoded in the UI.
    exchanges = []
    for name, prof in sorted(_registry.exchange_fees.items()):
        if name == "delta_india":
            # The registry row is frozen historical research evidence and does
            # not include India GST. The live cockpit must show the canonical
            # active Delta profile used by CostGate and shadow accounting.
            venue_model = CostModel.for_profile("delta_scalp")
            venue_config = venue_model.config
            exchanges.append(
                {
                    "exchange": prof.exchange,
                    "label": _EXCHANGE_LABELS.get(prof.exchange, prof.exchange),
                    "profile": venue_model.profile,
                    "maker_bps": round(
                        venue_config.maker_fee_bps * venue_config.fee_gst_mult, 3
                    ),
                    "taker_bps": round(
                        venue_config.taker_fee_bps * venue_config.fee_gst_mult, 3
                    ),
                    "slippage_bps": (
                        venue_config.default_slip_entry_bps
                        + venue_config.default_slip_exit_bps
                    ),
                    "safety_buffer_bps": venue_config.safety_buffer_bps,
                    "maker_first_cost_bps": round(
                        venue_model.round_trip_bps(maker_entry=True), 2
                    ),
                    "taker_round_trip_cost_bps": round(
                        venue_model.round_trip_bps(), 2
                    ),
                }
            )
            continue
        exchanges.append(
            {
                "exchange": prof.exchange,
                "label": _EXCHANGE_LABELS.get(prof.exchange, prof.exchange),
                "maker_bps": prof.maker_bps,
                "taker_bps": prof.taker_bps,
                "slippage_bps": prof.slippage_bps,
                "safety_buffer_bps": prof.safety_buffer_bps,
                "maker_first_cost_bps": round(prof.maker_first_cost_bps, 2),
                "taker_round_trip_cost_bps": round(prof.taker_round_trip_cost_bps, 2),
            }
        )
    return {
        "exchange": fee.exchange,
        "source": "vnedge.plan.cost_model canonical scalp profile",
        "profile": canonical.profile,
        "maker_bps": config.maker_fee_bps,
        "taker_bps": config.taker_fee_bps,
        "slippage_bps": config.default_slip_entry_bps + config.default_slip_exit_bps,
        "safety_buffer_bps": config.safety_buffer_bps,
        # Two labelled round-trip cost models (no safety buffer — the raw wall).
        "maker_first_rt_bps": round(maker_first_rt, 2),
        "taker_rt_bps": round(taker_rt, 2),
        # With the research safety buffer applied (what the gates actually use).
        "maker_first_cost_bps": round(canonical.round_trip_bps(maker_entry=True), 2),
        "taker_round_trip_cost_bps": round(canonical.round_trip_bps(), 2),
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
        self._published_at_monotonic: float | None = None

    def publish(self, snapshot: dict) -> None:
        self._latest = snapshot
        self._published_at_monotonic = time.monotonic()

    def latest(self) -> dict | None:
        return self._latest

    def age_seconds(self) -> float | None:
        if self._published_at_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self._published_at_monotonic)


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
    promotion_review_runbook_path: Path | None = None,
    lane_firing_causality_path: Path | None = None,
    paper_lane_activation_path: Path | None = None,
    paper_route_doctor_path: Path | None = None,
    paper_lane_cadence_path: Path | None = None,
    paper_lane_performance_path: Path | None = None,
    paper_trade_entry_autopsy_path: Path | None = None,
    paper_trade_exit_autopsy_path: Path | None = None,
    trade_analyzer_os_path: Path | None = None,
    paper_lane_root_cause_path: Path | None = None,
    maker_quote_lifecycle_path: Path | None = None,
    paper_trade_contract_reconciler_path: Path | None = None,
    paper_promotion_bridge_path: Path | None = None,
    lane_survival_path: Path | None = None,
    paper_lane_governor_path: Path | None = None,
    paper_roster_drift_path: Path | None = None,
    darwinian_agent_survival_path: Path | None = None,
    ml_pipeline_status_path: Path | None = None,
    pine_research_path: Path | None = None,
    quantified_strategy_lab_path: Path | None = None,
    quantified_port_factory_path: Path | None = None,
    quantified_blueprint_proof_path: Path | None = None,
    quantified_proof_arbiter_path: Path | None = None,
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
    strategy_workflow_path: Path | None = None,
    token_store: TokenStore | None = None,
    agent_token_store: AgentTokenStore | None = None,
    agent_audit_path: Path | None = None,
    agent_jobs_dir: Path | None = None,
    backtest_runs_path: Path | None = None,
    evidence_bundle_root_path: Path | None = None,
    evidence_bundle_catalog_path: Path | None = None,
    v2_dist_path: Path | None = None,
    session_issuer: SessionIssuer | None = None,
    quant_os_agent_gateway_dir: Path | None = None,
    market_pulse_service: MarketPulseService | None = None,
    settings_service: SettingsService | None = None,
    settings_path: Path | None = None,
    settings_audit_path: Path | None = None,
    session_cookie_secure: bool | None = None,
    fee_wall_forensics_path: Path | None = None,
) -> FastAPI:
    """Build the dashboard app with scoped, non-trading settings mutations.

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
    # Short-lived session tokens: present the root token once to POST /auth/session
    # to mint a JWT, then the root secret stops travelling on every request.
    issuer = session_issuer if session_issuer is not None else SessionIssuer.from_env()
    cookie_secure = (
        session_cookie_secure
        if session_cookie_secure is not None
        else os.environ.get("DASHBOARD_COOKIE_SECURE", "true").strip().lower()
        not in {"0", "false", "no"}
    )
    pulse_service = market_pulse_service or MarketPulseService(
        Path("data/candles"),
        Path("data/gaps"),
        Path("data/hour_analysis.sqlite"),
    )
    resolved_settings = settings_service or SettingsService(
        SettingsStore(settings_path or Path("data/settings.sqlite")),
        SecretBox.from_env(),
        OperatorAuditLog(settings_audit_path or Path("logs/settings_audit.jsonl")),
    )

    app = FastAPI(title="VNEDGE dashboard", docs_url=None, redoc_url=None)
    ws_connections: dict[str, int] = {}  # user name -> live socket count (never tokens)
    strategy_workflow_cache: dict | None = None
    strategy_workflow_cache_at = 0.0
    strategy_workflow_lock = asyncio.Lock()

    @app.middleware("http")
    async def spa_shell_cache_policy(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Never cache the React shell; hashed assets retain static caching."""
        response = await call_next(request)
        if request.url.path.rstrip("/") == "/app":
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    @app.get("/health")
    @app.get("/healthz")
    async def health() -> JSONResponse:
        """Unauthenticated liveness probe for container healthchecks + the TLS
        proxy. Returns 200 as soon as the app is serving; deliberately requires
        NO token and reveals no state — its only job is "is the process up".
        This is what lets compose gate dependents on `service_healthy` and stop
        the --force-recreate race that took the fleet down twice."""
        return JSONResponse({"status": "ok"})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Readiness means fresh runtime state and proven candle-lake health.

        Liveness remains deliberately shallow at ``/health``. This endpoint is
        the cross-process workflow contract: a running FastAPI process must not
        look ready while snapshots are stale, lanes are missing, the primary
        feed is stale, or the canonical-lake monitor has recorded holes.
        """
        snapshot = provider.latest()
        if snapshot is None:
            return JSONResponse(
                {"status": "not_ready", "reasons": ["snapshot_missing"]},
                status_code=503,
            )

        reasons: list[str] = []
        max_snapshot_age = float(
            os.environ.get("DASHBOARD_READY_MAX_SNAPSHOT_AGE_SECONDS", "30")
        )
        snapshot_age_ms = snapshot.get("snapshot_age_ms")
        try:
            snapshot_age = (
                float(snapshot_age_ms) / 1000.0
                if snapshot_age_ms is not None
                else provider.age_seconds()
                if hasattr(provider, "age_seconds")
                else None
            )
        except (TypeError, ValueError):
            snapshot_age = None
        if snapshot_age is None:
            reasons.append("snapshot_age_unknown")
        elif snapshot_age > max_snapshot_age:
            reasons.append("snapshot_stale")

        feed = snapshot.get("feed_health")
        if not isinstance(feed, dict):
            reasons.append("primary_feed_missing")
        elif str(feed.get("candles", "")).lower() != "ok":
            reasons.append("primary_feed_unhealthy")
        lane_health = snapshot.get("lane_health")
        if not isinstance(lane_health, dict):
            reasons.append("lane_health_missing")
        elif lane_health.get("process_healthy") is not True:
            reasons.append("lane_process_unhealthy")

        lake_path_raw = os.environ.get("CANDLE_LAKE_HEALTH_PATH", "").strip()
        if lake_path_raw:
            try:
                lake = json.loads(Path(lake_path_raw).read_text(encoding="utf-8"))
                if lake.get("status") != "healthy":
                    reasons.append("canonical_lake_unhealthy")
                checked_raw = lake.get("checked_at")
                checked_at = datetime.fromisoformat(checked_raw) if checked_raw else None
                if checked_at is not None and (
                    checked_at.tzinfo is None or checked_at.utcoffset() is None
                ):
                    checked_at = None
                max_lake_age = float(
                    os.environ.get("CANDLE_LAKE_HEALTH_MAX_AGE_SECONDS", "1200")
                )
                if checked_at is None or (
                    datetime.now(UTC) - checked_at.astimezone(UTC)
                ).total_seconds() > max_lake_age:
                    reasons.append("canonical_lake_check_stale")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                reasons.append("canonical_lake_status_unreadable")

        prerequisite_path = os.environ.get("SCANNER_PREREQ_HEALTH_PATH", "").strip()
        if prerequisite_path:
            try:
                prerequisite = json.loads(
                    Path(prerequisite_path).read_text(encoding="utf-8")
                )
                if prerequisite.get("status") != "ready":
                    reasons.append(
                        f"scanner_prerequisite_{prerequisite.get('status') or 'unknown'}"
                    )
                if prerequisite.get("arms_allowed") is not True:
                    reasons.append("scanner_arms_blocked")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                reasons.append("scanner_prerequisite_status_unreadable")

        if reasons:
            return JSONResponse(
                {"status": "not_ready", "reasons": sorted(set(reasons))},
                status_code=503,
            )
        return JSONResponse({"status": "ready", "reasons": []})

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
    backtest_reports_path = backtest_runs_path or Path("research/backtest_runs")
    backtest_artifacts_path = Path("research/live_research/agent_jobs")
    evidence_bundle_root = evidence_bundle_root_path or Path("research/evidence_bundles")
    evidence_bundle_catalog = evidence_bundle_catalog_path or (
        evidence_bundle_root / "index.sqlite"
    )
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
            ),
        )

    def _authorized(request: Request) -> AuthResult:
        """Authenticate the request; raise 401 (with the store's reason —
        e.g. expiry) on failure. Never returns an unauthorized result."""
        header = request.headers.get("authorization", "")
        candidate = header.removeprefix("Bearer ").strip()
        method = "bearer" if candidate else ""
        # URL credentials are disabled by default: query strings leak through
        # browser history, reverse-proxy logs, screenshots and referrers. A
        # temporary compatibility switch exists only for controlled migration
        # and is intentionally absent from the production compose contract.
        allow_query_token = os.environ.get(
            "DASHBOARD_ALLOW_QUERY_TOKEN", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not candidate and allow_query_token:
            candidate = request.query_params.get("token", "")
            method = "query" if candidate else ""
        if not candidate:
            candidate = request.cookies.get(_SESSION_COOKIE, "")
            method = "session_cookie" if candidate else ""
        # A short-lived session JWT is honored first; anything that isn't one of
        # ours (verify -> None) falls through to the long-lived token store, so
        # existing tokens keep working unchanged.
        session = issuer.verify(candidate)
        if session is not None:
            if not session.authorized:
                raise HTTPException(status_code=401, detail=session.reason or "invalid session")
            request.state.vnedge_auth_method = method
            return session
        result = store.authenticate(candidate)
        if not result.authorized:
            raise HTTPException(
                status_code=401, detail=result.reason or "missing or invalid token"
            )
        request.state.vnedge_auth_method = method or "root"
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

    def _require_settings(request: Request) -> AuthResult:
        return _require_permission(PERM_MANAGE_SETTINGS)(request)

    def _require_csrf(request: Request) -> None:
        """Double-submit CSRF protection for cookie-authenticated mutations.

        Explicit bearer clients are not vulnerable to ambient-cookie CSRF and
        remain usable for automation. Browser settings calls use the HttpOnly
        session cookie and must echo the non-secret CSRF cookie in a header.
        """
        if getattr(request.state, "vnedge_auth_method", "") != "session_cookie":
            return
        cookie = request.cookies.get(_CSRF_COOKIE, "")
        header = request.headers.get("x-vnedge-csrf", "")
        if not cookie or not header or not hmac.compare_digest(cookie, header):
            raise HTTPException(status_code=403, detail="missing or invalid CSRF token")

    def _issue_session_response(user: AuthResult) -> JSONResponse:
        session = issuer.issue(user.name or "", user.role or "viewer")
        csrf = secrets.token_urlsafe(32)
        response = JSONResponse(
            {
                "expires_at": session.expires_at.isoformat(),
                "name": user.name,
                "role": user.role,
                "rotated": True,
            },
            headers=_identity(user),
        )
        response.set_cookie(
            _SESSION_COOKIE,
            session.token,
            max_age=issuer.ttl_seconds,
            expires=session.expires_at,
            path="/",
            secure=cookie_secure,
            httponly=True,
            samesite="strict",
        )
        response.set_cookie(
            _CSRF_COOKIE,
            csrf,
            max_age=issuer.ttl_seconds,
            expires=session.expires_at,
            path="/",
            secure=cookie_secure,
            httponly=False,
            samesite="strict",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def _read_json_payload(path: Path | None, fallback: dict) -> dict:
        if path is None or not path.exists():
            return fallback
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return fallback  # mid-write race: serve a safe empty payload
        return payload if isinstance(payload, dict) else fallback

    def _artifact_payload(
        path: Path | None,
        fallback: dict,
        *,
        expected_interval_seconds: float | None = None,
        historical: bool = False,
    ) -> dict:
        """Read one artifact and attach current, server-computed provenance."""
        available = bool(path is not None and path.exists())
        payload = dict(_read_json_payload(path, fallback))
        now = datetime.now(UTC)
        source_as_of = payload.get("generated_at") or payload.get("generated_at_utc")
        if not source_as_of and available and path is not None:
            try:
                source_as_of = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=UTC
                ).isoformat()
            except OSError:
                available = False
        age_seconds: float | None = None
        if source_as_of:
            try:
                stamp = datetime.fromisoformat(str(source_as_of))
                if stamp.tzinfo is not None:
                    age_seconds = max(0.0, (now - stamp.astimezone(UTC)).total_seconds())
            except ValueError:
                age_seconds = None
        if not available:
            state = "MISSING"
        elif historical:
            state = "HISTORICAL"
        elif age_seconds is None:
            state = "UNKNOWN"
        elif expected_interval_seconds is not None and age_seconds > expected_interval_seconds:
            state = "STALE"
        else:
            state = "CURRENT"
        payload["artifact"] = {
            "available": available,
            "state": state,
            "served_at": now.isoformat(),
            "source_as_of": source_as_of,
            "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "expected_interval_seconds": expected_interval_seconds,
            "historical_evidence": historical,
        }
        return payload

    def _refresh_source_status(payload: dict) -> dict:
        """Never serve frozen source-health labels from an old agent artifact."""
        now = datetime.now(UTC)
        policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
        config = policy.get("config") if isinstance(policy.get("config"), dict) else {}
        stale_minutes = float(config.get("stale_artifact_minutes") or 120.0)
        rows = payload.get("source_status")
        refreshed = []
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            generated = row.get("generated_at")
            age_minutes: float | None = None
            if generated:
                try:
                    stamp = datetime.fromisoformat(str(generated))
                    if stamp.tzinfo is not None:
                        age_minutes = max(
                            0.0, (now - stamp.astimezone(UTC)).total_seconds() / 60.0
                        )
                except ValueError:
                    pass
            row["age_minutes"] = round(age_minutes, 2) if age_minutes is not None else None
            row["state"] = (
                "MISSING"
                if age_minutes is None
                else "STALE"
                if age_minutes > stale_minutes
                else "OK"
            )
            refreshed.append(row)
        payload["source_status"] = refreshed
        return payload

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
    quantified_blueprint_proof_file = (
        quantified_blueprint_proof_path
        or Path("research/live_research/quantified_blueprint_proof_latest.json")
    )
    quantified_proof_arbiter_file = (
        quantified_proof_arbiter_path
        or Path("research/live_research/quantified_proof_result_arbiter_latest.json")
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
    fee_wall_forensics_file = (
        fee_wall_forensics_path
        or Path("research/live_research/fee_wall_forensics_latest.json")
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
    strategy_workflow_file = (
        strategy_workflow_path
        or Path("research/live_research/strategy_workflow_latest.json")
    )
    strategy_workflow_override = strategy_workflow_path is not None
    paper_lane_activation_file = (
        paper_lane_activation_path
        or Path("research/live_research/paper_lane_activation_latest.json")
    )
    promotion_review_runbook_file = (
        promotion_review_runbook_path
        or Path("research/live_research/promotion_review_runbook_latest.json")
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
    trade_analyzer_os_file = (
        trade_analyzer_os_path
        or Path("research/live_research/trade_analyzer_os_latest.json")
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
    paper_promotion_bridge_file = (
        paper_promotion_bridge_path
        or Path("research/live_research/paper_promotion_bridge_latest.json")
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
    async def index() -> RedirectResponse:
        """Canonical operator UI.

        The old monolithic dashboard is intentionally retired from navigation;
        keeping two cockpits caused schema drift and contradictory health labels.
        """
        return RedirectResponse(
            url="/app/",
            status_code=307,
            headers={"Cache-Control": "no-store, must-revalidate"},
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
        return JSONResponse(
            {**snapshot, "build_sha": _build_sha()},
            headers=_identity(user),
        )

    @app.get("/api/lanes")
    async def correction_lanes(request: Request) -> JSONResponse:
        """Policy-labelled active roster for the read-only React cockpit."""
        user = _authorized(request)
        snapshot = provider.latest()
        if snapshot is None:
            return JSONResponse(
                {"status": "no snapshot yet"},
                status_code=503,
                headers=_identity(user),
            )
        return JSONResponse(build_lanes_payload(snapshot), headers=_identity(user))

    @app.get("/api/risk/snapshot")
    async def correction_risk(request: Request) -> JSONResponse:
        """Truthful kill, halt, journal, stream, and live-block posture."""
        user = _authorized(request)
        snapshot = provider.latest()
        if snapshot is None:
            return JSONResponse(
                {"status": "no snapshot yet"},
                status_code=503,
                headers=_identity(user),
            )
        return JSONResponse(
            build_risk_payload({**snapshot, "build_sha": _build_sha()}),
            headers=_identity(user),
        )

    @app.get("/api/scanners")
    async def scanner_catalog(request: Request) -> JSONResponse:
        """Browsable catalogue of every scanner and the evidence behind it.

        Ordered by evidence state, not by profit. Ranking a strategy directory
        by best return is how the luckiest curve fit reaches the top row.
        """
        user = _authorized(request)
        payload = await asyncio.to_thread(live_catalog, Path("docs/prereg"))
        return JSONResponse(payload, headers=_identity(user))

    @app.get("/api/candles/{symbol}")
    async def chart_candles(
        symbol: str,
        request: Request,
        exchange: str = "binanceusdm",
        timeframe: str = "1h",
        n: int = 500,
    ) -> JSONResponse:
        """Canonical OHLCV for the chart.

        Deliberately reads the SAME store research and shadow are meant to
        read. A separate UI feed would make the cockpit a fourth candle source
        on top of the three that already disagree.
        """
        user = _authorized(request)
        store = CandleParquetStore(Path("data/candles"), exchange=exchange)
        payload = await asyncio.to_thread(
            candles_payload, store, symbol, timeframe, limit=n
        )
        return JSONResponse(payload, headers=_identity(user))

    @app.get("/api/candles/{symbol}/context")
    async def chart_mechanism_context(
        symbol: str,
        request: Request,
        exchange: str = "binanceusdm",
        timeframe: str = "1h",
        n: int = 600,
    ) -> JSONResponse:
        """Drawable mechanism context (swing levels, channel, FVG zones).

        Computed by the ML plane's own definitions over the SAME canonical
        store as the candles endpoint — the chart and the model can never
        describe two different markets. Presentation-only.
        """
        user = _authorized(request)
        store = CandleParquetStore(Path("data/candles"), exchange=exchange)
        payload = await asyncio.to_thread(
            mechanism_context_payload, store, symbol, timeframe, limit=n
        )
        return JSONResponse(payload, headers=_identity(user))

    @app.get("/api/pulse/{symbol}")
    async def market_pulse(
        symbol: str,
        request: Request,
        exchange: str = "binanceusdm",
        n: int = 48,
    ) -> JSONResponse:
        """Coalesced read-only pulse: closed hours, forming state, book, and alerts."""
        user = _authorized(request)
        payload = await asyncio.to_thread(
            pulse_service.pulse,
            exchange,
            symbol,
            limit=n,
            runtime=provider.latest(),
        )
        return JSONResponse(payload, headers=_identity(user))

    @app.get("/api/pulse/{symbol}/hours")
    async def market_pulse_hours(
        symbol: str,
        request: Request,
        exchange: str = "binanceusdm",
        n: int = 48,
    ) -> JSONResponse:
        """Closed UTC-hour strip projection, bounded to seven days."""
        user = _authorized(request)
        payload = await asyncio.to_thread(pulse_service.hours, exchange, symbol, limit=n)
        return JSONResponse(payload, headers=_identity(user))

    @app.get("/api/pulse/{symbol}/hours/{open_time}/analysis")
    async def market_pulse_analysis(
        symbol: str,
        open_time: str,
        request: Request,
        exchange: str = "binanceusdm",
    ) -> JSONResponse:
        """Cached fixed-schema observation brief for one closed hour."""
        user = _authorized(request)
        try:
            parsed = datetime.fromisoformat(open_time)
            payload = await asyncio.to_thread(
                pulse_service.analysis,
                exchange,
                symbol,
                parsed,
            )
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(payload, headers=_identity(user))

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

    @app.post("/auth/session")
    async def auth_session(request: Request) -> JSONResponse:
        """Exchange the (long-lived) root token for a short-lived session JWT.

        Authenticates the presented token exactly like any data route, then mints
        a JWT carrying the same identity/role. The JWT is set only as an
        HttpOnly cookie, so browser JavaScript and URLs never receive it. The
        root secret stops travelling after this request. Read-only: this grants
        no new capability — the session's role equals the token's."""
        return _issue_session_response(_authorized(request))

    @app.post("/auth/session/refresh")
    async def refresh_auth_session(request: Request) -> JSONResponse:
        """Renew an active browser session without reusing the root token.

        The HttpOnly session cookie proves the caller's identity and the
        double-submit CSRF token proves the request came from the dashboard.
        Renewal preserves the existing role and mints no new capability. An
        expired session cannot be revived; the operator must authenticate with
        the root token again.
        """
        user = _authorized(request)
        _require_csrf(request)
        return _issue_session_response(user)

    mount_settings_routes(
        app,
        service=resolved_settings,
        authorize=_require_settings,
        require_csrf=_require_csrf,
        issue_session=_issue_session_response,
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

    def _orphan_lane_ids() -> set[str]:
        """Lane ids the auditor flagged ORPHAN — a journal file with no desired
        spec, left behind by a config change. It represents nothing live, so the
        per-lane side endpoints must not serve it as if it were a real lane."""
        snap = provider.latest() or {}
        health = snap.get("lane_health") or {}
        return {
            str(p.get("lane_id"))
            for p in (health.get("problems") or [])
            if p.get("verdict") == "ORPHAN" and p.get("lane_id")  # VERDICT_ORPHAN
        }

    def _reject_if_orphan(lane: str) -> None:
        """Filter orphan lanes out of the side endpoints (empty lane = primary,
        never orphan). A config leftover must not look like a queryable lane."""
        if lane and lane in _orphan_lane_ids():
            raise HTTPException(
                status_code=409,
                detail=(f"lane '{lane}' is an ORPHAN (journal leftover from a "
                        "config change) — not a live lane; nothing to serve"),
            )

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
        _reject_if_orphan(lane)
        since = _since_iso(_query_days(request))
        try:
            limit = max(1, min(int(request.query_params.get("limit", "5000")), 20_000))
        except ValueError:
            raise HTTPException(status_code=400, detail="limit must be an integer")
        return JSONResponse(_equity_points(lane, since)[-limit:], headers=_identity(user))

    @app.get("/export.csv")
    async def export_csv(request: Request) -> Response:
        """Per-lane CSV export: equity curve + trade log + fills, one flat
        table keyed by record_type. Same filters as /history."""
        user = _authorized(request)
        lane = _query_lane(request)
        _reject_if_orphan(lane)
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
    async def trade_journal(
        request: Request, limit: str = "200", offset: str = "0"
    ) -> JSONResponse:
        """Read-only trade journal projection.

        Combines current snapshot positions/orders with per-lane decision
        journals and hash-chained fill ledgers. No controls, no mutations.
        """
        user = _authorized(request)
        lane = _query_lane(request)
        _reject_if_orphan(lane)
        since = _since_iso(_query_days(request))
        try:
            limit = max(1, min(int(limit), 500))
        except ValueError:
            raise HTTPException(status_code=400, detail="limit must be an integer")
        try:
            offset = max(0, int(offset))
        except ValueError:
            raise HTTPException(status_code=400, detail="offset must be an integer")
        return JSONResponse(
            build_trade_journal(
                snapshot=provider.latest(),
                journal_dir=lane_dir,
                history_path=history_path,
                scanner_evidence_path=Path(
                    "research/live_research/scanner_evidence_latest.json"
                ),
                lane=lane,
                since=since,
                limit=limit,
                offset=offset,
            ),
            headers=_identity(user),
        )

    @app.get("/session-regime")
    async def session_regime(request: Request, limit: str = "4000") -> JSONResponse:
        """Session-regime rollup: closed trades bucketed by UTC entry session.

        Answers *when* each strategy earns (asia/europe/us/late) — trades, win
        rate, net $, worst stretch, break-even cushion per (strategy x session).
        Recent-window view over the same active-lane-filtered ledgers as
        /trade-journal. Read-only, no controls.
        """
        user = _authorized(request)
        lane = _query_lane(request)
        _reject_if_orphan(lane)
        since = _since_iso(_query_days(request))
        try:
            limit = max(1, min(int(limit), 20000))
        except ValueError:
            raise HTTPException(status_code=400, detail="limit must be an integer")
        return JSONResponse(
            build_session_regime(
                snapshot=provider.latest(),
                journal_dir=lane_dir,
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
        merged = (
            _alert_incidents(alert_files)
            + _journal_incidents(lane_dir)
            + _snapshot_health_incidents(provider.latest())
        )
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

    @app.get("/backtest-lab")
    async def backtest_lab(request: Request) -> JSONResponse:
        """Professional read model for queued and completed backtest runs.

        Execution deliberately remains in the bounded Agent Gateway worker;
        this route only renders durable evidence and cannot trade or promote.
        """
        user = _authorized(request)
        selected = request.query_params.get("run_id", "").strip() or None
        if selected is not None and not re.fullmatch(r"[A-Za-z0-9_.:@+-]{1,240}", selected):
            raise HTTPException(status_code=400, detail="invalid backtest run id")
        payload = load_backtest_lab(
            jobs_dir=agent_jobs_path,
            reports_dir=backtest_reports_path,
            artifact_dir=backtest_artifacts_path,
            evidence_bundle_dir=evidence_bundle_root,
            evidence_index_path=evidence_bundle_catalog,
            selected_run_id=selected,
        )
        return JSONResponse(payload, headers=_identity(user))

    @app.post("/backtest-lab/runs", status_code=202)
    async def queue_backtest_lab_run(
        request: Request,
        payload: BacktestRequest,
    ) -> JSONResponse:
        """Queue one bounded, research-only run for the external worker.

        This is the only Backtest Lab mutation. It cannot execute inline,
        alter source, create a runtime lane, promote a strategy, or route an
        order. Operator permission and browser CSRF protection are mandatory.
        """
        user = _require_permission(PERM_REQUEST_BACKTEST)(request)
        _require_csrf(request)
        # The worker remains authoritative, but rejecting obvious catalog
        # mistakes here prevents a useless queued job from looking valid.
        from vnedge.strategy.strategy_registry import STRATEGIES

        if payload.strategy_id not in STRATEGIES:
            raise HTTPException(status_code=422, detail="strategy is not registered")
        job = create_backtest_job(
            jobs_dir=agent_jobs_path,
            agent=f"dashboard:{user.name or 'operator'}",
            request=payload.model_dump(mode="json"),
        )
        return JSONResponse(job, status_code=202, headers=_identity(user))

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
        payload = _artifact_payload(
                agentic_research_os_file,
                {
                    "artifact_available": False,
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
                expected_interval_seconds=2 * 60 * 60,
            )
        return JSONResponse(
            _refresh_source_status(payload), headers=_identity(user)
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

    @app.get("/promotion-review-runbook")
    async def promotion_review_runbook(request: Request) -> JSONResponse:
        """Latest promotion review runbook.

        This is the operator packet derived from red-team prosecution of PASSED
        walk-forward candidates. It never promotes or trades.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                promotion_review_runbook_file,
                {
                    "runbook_id": "promotion_review_runbook_v1",
                    "summary": {},
                    "rows": [],
                    "operator_answer": "promotion review runbook unavailable",
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
            _artifact_payload(
                ml_pipeline_status_file,
                {
                    "artifact_available": False,
                    "stage": "UNAVAILABLE",
                    "stages": [],
                    "dataset": {"samples": 0, "min_to_train": 200, "progress_pct": 0.0, "by_strategy": {}},
                    "foundation": {},
                    "gates": {},
                    "online_shadow": {
                        "library": "river",
                        "installed": False,
                        "configured": False,
                        "active": False,
                        "binding": False,
                        "can_trade": False,
                        "note": "River shadow status unavailable",
                    },
                    "model": None,
                    "can_trade": False,
                    "can_promote": False,
                    "note": "ml pipeline status unavailable",
                },
                expected_interval_seconds=2 * 60 * 60,
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
        payload = build_pre_live_status(
            journal_dir=lane_dir or Path("logs/paper_trials"),
            ladder_path=ladder if ladder.exists() else None,
        )
        snapshot = provider.latest() or {}
        runtime_checklist = build_risk_payload(snapshot)["live_checklist"]
        owner_by_id = {
            "live_flags": "deliberate",
            "trade_keys": "operator",
        }
        payload["checks"] = [
            {
                "name": item["id"],
                "passed": bool(item["ok"]),
                "critical": True,
                "detail": f"runtime snapshot: {item['label']}",
                "owner": owner_by_id.get(item["id"], "system"),
            }
            for item in runtime_checklist["items"]
        ]
        payload["red_count"] = runtime_checklist["total"] - runtime_checklist["passed"]
        payload["cleared"] = payload["red_count"] == 0
        payload["operator_action_reds"] = [
            item["name"]
            for item in payload["checks"]
            if not item["passed"] and item["owner"] == "operator"
        ]
        payload["operator_answer"] = (
            "all runtime live gates cleared"
            if payload["cleared"]
            else f"{payload['red_count']} runtime gate(s) still red — see the path to live"
        )
        return JSONResponse(
            payload,
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

    @app.get("/trade-analyzer-os")
    async def trade_analyzer_os(request: Request) -> JSONResponse:
        """Latest joined trade analyzer verdict.

        This joins paper trade journal, entry autopsy, and exit autopsy into one
        read-only operator answer. It cannot promote, demote, or trade.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                trade_analyzer_os_file,
                {
                    "summary": {},
                    "rows": [],
                    "recent_trades": [],
                    "operator_answer": "trade analyzer OS unavailable",
                    "mode": "read_only_trade_analyzer_os",
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

    @app.get("/paper-promotion-bridge")
    async def paper_promotion_bridge(request: Request) -> JSONResponse:
        """Latest joined paper/live-review bridge.

        This joins lane readiness, paper performance, contract truth,
        maker/taker lifecycle, and operator actions into a single conservative
        review answer. It is read-only and cannot promote or trade.
        """
        user = _authorized(request)
        return JSONResponse(
            _read_json_payload(
                paper_promotion_bridge_file,
                {
                    "summary": {},
                    "boards": {},
                    "rows": [],
                    "operator_answer": "paper promotion bridge unavailable",
                    "mode": "read_only_paper_promotion_bridge",
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
                    "mode": "read_only_unified_lane_roster",
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
        disk = shutil.disk_usage(Path.cwd())
        load = os.getloadavg() if hasattr(os, "getloadavg") else (None, None, None)
        forwarded_proto = request.headers.get("x-forwarded-proto")
        secure = request.url.scheme == "https" or forwarded_proto == "https"
        return JSONResponse(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "build_sha": _build_sha(),
                "host": os.environ.get("VNEDGE_HOST") or socket.gethostname(),
                "uptime_seconds": int(max(0.0, time.time() - _APP_START)),
                "process_id": os.getpid(),
                "python": os.sys.version.split()[0],
                "cpu_count": os.cpu_count(),
                "load_average": {"1m": load[0], "5m": load[1], "15m": load[2]},
                "disk": {
                    "total_bytes": disk.total,
                    "used_bytes": disk.used,
                    "free_bytes": disk.free,
                    "used_pct": round(100.0 * disk.used / disk.total, 2),
                },
                "transport": {
                    "scheme": request.url.scheme,
                    "secure": secure,
                    "forwarded_proto": forwarded_proto,
                },
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

    @app.get("/data-products")
    async def data_products(request: Request) -> JSONResponse:
        """Current provenance for runtime, canonical and optional evidence products."""
        user = _authorized(request)
        scanner = _artifact_payload(
            Path("research/live_research/scanner_evidence_latest.json"),
            {},
            expected_interval_seconds=15 * 60,
        )["artifact"]
        quote_parity = _artifact_payload(
            Path("research/live_research/quote_parity_status.json"),
            {},
            expected_interval_seconds=30 * 60,
        )["artifact"]
        maintenance = _artifact_payload(
            Path("data/reports/tick_lake_maintenance.json"),
            {},
            expected_interval_seconds=25 * 60 * 60,
        )["artifact"]
        recovery = _artifact_payload(
            Path("data/reports/binance_gap_recovery.json"),
            {},
            expected_interval_seconds=20 * 60,
        )["artifact"]
        ml_artifact = _artifact_payload(
            ml_pipeline_status_file, {}, expected_interval_seconds=2 * 60 * 60
        )["artifact"]
        agent_artifact = _artifact_payload(
            agentic_research_os_file, {}, expected_interval_seconds=2 * 60 * 60
        )["artifact"]
        score_artifact = _artifact_payload(
            fee_wall_forensics_file, {}, historical=True
        )["artifact"]
        snapshot_age = provider.age_seconds()
        rows = [
            {
                "product": "runtime_snapshot",
                "class": "runtime",
                "required": True,
                "state": "CURRENT" if snapshot_age is not None and snapshot_age <= 15 else "STALE",
                "age_seconds": snapshot_age,
                "expected_interval_seconds": 15,
                "source_as_of": None,
            },
            {"product": "scanner_evidence", "class": "derived", "required": False, **scanner},
            {
                "product": "quote_parity",
                "class": "cutover_evidence",
                "required": False,
                **quote_parity,
            },
            {"product": "gap_recovery", "class": "canonical_recovery", "required": True, **recovery},
            {"product": "tick_lake_maintenance", "class": "maintenance", "required": True, **maintenance},
            {"product": "ml_pipeline", "class": "optional_research", "required": False, **ml_artifact},
            {"product": "agent_governor", "class": "optional_research", "required": False, **agent_artifact},
            {"product": "research_scorecard", "class": "historical_evidence", "required": False, **score_artifact},
        ]
        return JSONResponse(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "rows": rows,
                "required_non_current": sum(
                    row["required"] and row["state"] != "CURRENT" for row in rows
                ),
                "read_only": True,
            },
            headers=_identity(user),
        )

    @app.get("/scanner-evidence")
    async def scanner_evidence(request: Request) -> JSONResponse:
        """Daily exact-ID scanner evaluations and near-miss evidence.

        The artifact is generated by a credential-free read-only service and
        contains no mutation, promotion, or order controls.
        """
        user = _authorized(request)
        payload = _artifact_payload(
            Path("research/live_research/scanner_evidence_latest.json"),
            {
                "schema_version": 2,
                "generated_at": None,
                "read_only": True,
                "evaluations": 0,
                "fires": 0,
                "strategies": [],
                "status": "artifact_unavailable",
            },
            expected_interval_seconds=15 * 60,
        )
        return JSONResponse(payload, headers=_identity(user))

    @app.get("/quote-parity")
    async def quote_parity(request: Request) -> JSONResponse:
        """Read-only live-versus-replay evidence for lane-consumed BBO.

        ``cutover_ready`` is an evidence result only. It cannot switch the
        canonical producer, enable router decision authority, or grant orders.
        """
        user = _authorized(request)
        payload = _artifact_payload(
            Path("research/live_research/quote_parity_status.json"),
            {
                "schema_version": 1,
                "generated_at": None,
                "read_only": True,
                "authority_changed": False,
                "router_decision_authority": False,
                "capital_enabled": False,
                "summary": {
                    "lanes": 0,
                    "applicable_lanes": 0,
                    "statuses": {},
                    "cutover_ready": False,
                },
                "lanes": [],
            },
            expected_interval_seconds=30 * 60,
        )
        return JSONResponse(payload, headers=_identity(user))

    @app.get("/strategy-workflow")
    async def strategy_workflow(request: Request) -> JSONResponse:
        """Immutable strategy versions, lineage, evidence, and quarantine state.

        The endpoint is intentionally GET-only.  Registration, forking, and
        quarantine are reviewed CLI/library operations; this surface cannot
        grant shadow/capital permission or promote a strategy.
        """
        user = _authorized(request)
        if strategy_workflow_override:
            payload = _read_json_payload(
                strategy_workflow_file,
                {
                    "workflow_id": "strategy_workflow_v1",
                    "status": "artifact_unreadable",
                    "summary": {},
                    "revisions": [],
                    "policy": {"can_trade": False, "can_promote": False},
                },
            )
        else:
            nonlocal strategy_workflow_cache, strategy_workflow_cache_at
            ttl = max(
                1.0,
                float(os.environ.get("DASHBOARD_STRATEGY_WORKFLOW_CACHE_SECONDS", "60")),
            )
            async with strategy_workflow_lock:
                cache_age = time.monotonic() - strategy_workflow_cache_at
                if strategy_workflow_cache is not None and cache_age < ttl:
                    payload = {**strategy_workflow_cache}
                else:
                    try:
                        payload = await asyncio.wait_for(
                            asyncio.to_thread(build_strategy_workflow),
                            timeout=10.0,
                        )
                        strategy_workflow_cache = payload
                        strategy_workflow_cache_at = time.monotonic()
                    except (OSError, ValueError, TimeoutError) as exc:
                        payload = _read_json_payload(
                            strategy_workflow_file,
                            {
                                "workflow_id": "strategy_workflow_v1",
                                "status": "workflow_unavailable",
                                "error": str(exc) or type(exc).__name__,
                                "summary": {},
                                "revisions": [],
                                "policy": {
                                    "can_trade": False,
                                    "can_promote": False,
                                },
                            },
                        )
        payload["can_trade"] = False
        payload["can_promote"] = False
        return JSONResponse(payload, headers=_identity(user))

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
                    "samples": 0, "samples_total": 0, "venues": set(),
                },
            )
            if r.get("exchange"):
                g["venues"].add(r["exchange"])
            disclosure = performance_disclosure(summ, r)
            g["samples_total"] += disclosure["samples"]
            if g["best_net_bps"] is None or net > g["best_net_bps"]:
                g["best_net_bps"] = net
                g.update(disclosure)
                g["break_rate_pct"] = summ.get("fee_wall_break_rate_pct")
        rows = []
        for g in by.values():
            g = dict(g)
            g["venues"] = sorted(v for v in g["venues"] if v)
            rows.append(g)
        rows.sort(
            key=lambda r: (
                not r.get("sample_qualified", False),
                r["best_net_bps"] is None,
                -(r["best_net_bps"] or -1e9),
            )
        )
        snapshot = provider.latest() or {}
        active_payload = build_lanes_payload(snapshot)
        evidence_ids = {str(row.get("strategy")) for row in rows if row.get("strategy")}
        aligned: dict[str, dict] = {}
        for lane in active_payload.get("lanes", []):
            if lane.get("observation_class") != "shadow_observe":
                continue
            strategy_id = str(lane.get("strategy_id") or "unknown")
            item = aligned.setdefault(
                strategy_id,
                {
                    "strategy_id": strategy_id,
                    "lane_count": 0,
                    "symbols": set(),
                    "timeframes": set(),
                    "resolved_outcomes": 0,
                    "pending_intents": 0,
                    "scorecard_match": strategy_id in evidence_ids,
                },
            )
            item["lane_count"] += 1
            if lane.get("symbol"):
                item["symbols"].add(str(lane["symbol"]))
            if lane.get("timeframe"):
                item["timeframes"].add(str(lane["timeframe"]))
            perf = lane.get("shadow_perf") or {}
            item["resolved_outcomes"] += int(perf.get("wins") or 0) + int(
                perf.get("losses") or 0
            )
            item["pending_intents"] += int(perf.get("pending_shadow_intents") or 0)
        runtime_alignment = []
        for item in aligned.values():
            item = dict(item)
            item["symbols"] = sorted(item["symbols"])
            item["timeframes"] = sorted(item["timeframes"])
            if item["scorecard_match"]:
                item["status"] = "EVIDENCE_MATCH"
            elif item["resolved_outcomes"]:
                item["status"] = "RUNTIME_OUTCOMES_NOT_SCORED"
            else:
                item["status"] = "NO_CURRENT_EVIDENCE"
            runtime_alignment.append(item)
        runtime_alignment.sort(key=lambda item: item["strategy_id"])
        return JSONResponse(
            {
                "generated_at": forensics.get("generated_at"),
                "strategies": rows,
                "probes": probes.get("paper_probes", []),
                "probe_actuals": probe_actuals.get("rows", []),
                "probe_actuals_summary": probe_actuals.get("summary", {}),
                "performance_policy": scorecard_policy(),
                "runtime_alignment": runtime_alignment,
                "can_trade": False,
                "can_promote": False,
                "artifact": _artifact_payload(
                    fee_wall_forensics_file,
                    {"reports": []},
                    historical=True,
                )["artifact"],
            }
        )

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

    @app.get("/quantified-strategy-lab/blueprint-proof")
    async def quantified_strategy_lab_blueprint_proof(request: Request) -> JSONResponse:
        """Research-only proof matrix for every Quantified blueprint."""
        user = _authorized(request)
        return JSONResponse(
            load_quantified_blueprint_proof_payload(quantified_blueprint_proof_file),
            headers=_identity(user),
        )

    @app.get("/quantified-strategy-lab/proof-arbiter")
    async def quantified_strategy_lab_proof_arbiter(request: Request) -> JSONResponse:
        """Research-only next-action arbiter for Quantified proof cells."""
        user = _authorized(request)
        return JSONResponse(
            load_quantified_proof_result_arbiter_payload(quantified_proof_arbiter_file),
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

    @app.websocket("/api/pulse/stream")
    async def market_pulse_stream(websocket: WebSocket) -> None:
        """Five-second coalesced pulse stream; never forwards individual ticks."""
        candidate = websocket.cookies.get(_SESSION_COOKIE, "")
        result = issuer.verify(candidate) or store.authenticate(candidate)
        if not result.authorized:
            await websocket.close(
                code=4401,
                reason=(result.reason or "missing or invalid token")[:120],
            )
            return
        symbol = websocket.query_params.get("symbol", "BTCUSDT")
        exchange = websocket.query_params.get("exchange", "binanceusdm")
        await websocket.accept()
        try:
            while True:
                if result.expires_at is not None and datetime.now(UTC) >= result.expires_at:
                    await websocket.close(code=4401, reason="token expired")
                    return
                payload = await asyncio.to_thread(
                    pulse_service.pulse,
                    exchange,
                    symbol,
                    limit=48,
                    runtime=provider.latest(),
                )
                await websocket.send_json(payload)
                await asyncio.sleep(5)
        except (WebSocketDisconnect, ConnectionError):
            return
        except Exception as exc:  # noqa: BLE001 — browser isolation boundary
            logger.warning("market pulse websocket dropped: %s", exc)
            return

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        candidate = websocket.cookies.get(_SESSION_COOKIE, "")
        result = issuer.verify(candidate) or store.authenticate(candidate)
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
                    datetime.now(UTC) >= result.expires_at
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

    # v2 React frontend (frontend/dist), served at /app as a static SPA. Mounted
    # ONLY when a build exists — so a production image without the build simply
    # has no /app route (never a 500), and the classic dashboard at / is
    # unaffected. The SPA shell is public like the classic shell; its data calls
    # (/state, /journal, /whoami) stay token-gated. Build: `npm --prefix
    # frontend install && npm --prefix frontend run build`.
    # Resolve the built SPA across both layouts, like the runbooks doc above:
    # dev (repo checkout → _REPO_ROOT/frontend/dist) and the container (vnedge is
    # pip-installed into site-packages, so _REPO_ROOT points there; the build is
    # COPYed to /app/frontend/dist == cwd/frontend/dist).
    if v2_dist_path is not None:
        v2_candidates = [Path(v2_dist_path)]
    else:
        v2_candidates = [Path.cwd() / "frontend" / "dist", _REPO_ROOT / "frontend" / "dist"]
    v2_dist = next((c for c in v2_candidates if c.is_dir()), None)
    if v2_dist is not None:
        from starlette.staticfiles import StaticFiles

        app.mount("/app", StaticFiles(directory=str(v2_dist), html=True), name="v2")
        logger.info("v2 frontend mounted at /app from %s", v2_dist)

    return app
