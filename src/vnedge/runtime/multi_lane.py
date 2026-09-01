"""Multi-exchange shadow lanes.

Runs the same (or different) strategy as parallel, fully isolated shadow
lanes across venues — e.g. funding-MR on Binance/Bybit and a candle-only
Delta lane — to answer: does the strategy behave better on one venue under
live markets?

Each lane is a complete, independent LivePaperSession:
- its own live feed (real venue websockets), simulated exchange, gateway,
  order manager, journal, account store, equity history, and $ shadow base.
- NO live orders, NO cross-venue routing. Pure per-venue shadow.

The dashboard sees the PRIMARY (governed) lane as the flat top-level
snapshot (backward-compatible), plus a `lanes` array for side-by-side
comparison. One venue's feed stalling never blocks another lane.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Protocol

import pandas as pd
from ccxt.base.errors import NetworkError, NotSupported

from vnedge.config.risk_config import ABSOLUTE_MAX_LEVERAGE, RiskConfig
from vnedge.dashboard.health_bands import annotate
from vnedge.data.candles import CandleParquetStore
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.data_quality_gate import validate_candles
from vnedge.data.gaps import GapParquetStore
from vnedge.data.schemas import normalize_candles, normalize_funding
from vnedge.data.symbols import canonical_symbol
from vnedge.exchange.feed_registry import SharedFeedView, acquire_market_feed
from vnedge.exchange.live_feed import LiveMarketFeed, RestPollingMarketFeed
from vnedge.exchange.venue_specs import venue_fill_model, venue_symbol_limits
from vnedge.execution.fill_ledger import FillLedger
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.signal_arbiter import ArbiterConfig, SignalArbiter
from vnedge.paper.account_store import PaperAccountStore
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.canonical_candle_router import (
    CanonicalCandleRouter,
    CanonicalCandleSubscription,
    warm_subscription_from_store,
)
from vnedge.runtime.daily_factory import DailySignalFactoryConfig
from vnedge.runtime.funnel_store import LaneFunnelStore
from vnedge.runtime.latency_store import LaneLatencyStore, RecorderLatencyStore
from vnedge.runtime.live_paper import LivePaperSession
from vnedge.runtime.paper_trial import LiveFundingMR
from vnedge.runtime.quote_evidence import QuoteEvidenceRecorder
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.runtime.shadow_portfolio import ShadowPortfolioGate
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.composite import CompositeSignalStrategy
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin
from vnedge.strategy.fee_wall_momentum_observer import FeeWallMomentumObserver
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.htf_regime_continuation_15m import HtfRegimeContinuation15mV1
from vnedge.strategy.htf_regime_continuation_15m_v2 import HtfRegimeContinuation15mV2
from vnedge.strategy.measurement_only import MeasurementOnly
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.range_expansion_observer import RangeExpansionObserver
from vnedge.strategy.range_expansion_observer_v2 import RangeExpansionObserverV2
from vnedge.strategy.range_expansion_observer_v3 import RangeExpansionObserverV3
from vnedge.strategy.range_expansion_observer_v4 import RangeExpansionObserverV4
from vnedge.strategy.realtime_scanners import REALTIME_SCANNERS
from vnedge.strategy.research_scanners import NEW_RESEARCH_SCANNERS
from vnedge.strategy.scanner_contracts import scanner_runtime_contract
from vnedge.strategy.squeeze_expansion_breakout import SqueezeExpansionBreakout
from vnedge.strategy.squeeze_expansion_breakout_v3 import SqueezeExpansionBreakoutV3
from vnedge.strategy.squeeze_expansion_breakout_v4 import SqueezeExpansionBreakoutV4
from vnedge.strategy.strategy_registry import is_capital_eligible
from vnedge.strategy.structure_bos_1h import StructureBos1H
from vnedge.strategy.structure_bos_15m_trigger_v2 import StructureBos15mTriggerV2
from vnedge.strategy.structure_bos_15m_trigger_v3 import StructureBos15mTriggerV3
from vnedge.strategy.trend_continuation import TrendContinuation
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout

logger = logging.getLogger(__name__)


class CanonicalProducer(Protocol):
    """Minimal lifecycle owned by the colocated dark runtime."""

    async def run(self) -> None: ...

    def new_arm_block_reason(self, symbol: str) -> str | None: ...


@dataclass(frozen=True)
class LaneSpec:
    lane_id: str  # unique, e.g. "binance_funding_mr"
    exchange: str  # ccxt id, e.g. "binanceusdm" | "bybit"
    symbol: str
    timeframe: str = "1h"
    starting_equity: float = 500.0
    strategy_params: dict | None = None
    is_primary: bool = False  # the governed lane shown as the flat snapshot
    daily_loss_usd: float = 10.0
    mode: RunnerMode = RunnerMode.SHADOW
    strategy_id: str = "measurement_only_v1"
    #: Dynamic ATR-chandelier trail on the active-exit remainder (0.0 = off, the
    #: legacy arm-and-lock). Set per-lane when a strategy was JUDGED with a trail,
    #: so the running lane uses the exit its promotion evidence was measured on.
    trail_atr_mult: float = 0.0
    # Public-data venue is not an execution-cost assumption. Shadow scanners
    # may observe Binance while conservatively modelling Delta India fees.
    execution_cost_exchange: str | None = None

    @property
    def data_symbol(self) -> str:
        """Stable storage/router identity; ``symbol`` remains venue-native."""
        return canonical_symbol(self.symbol)

    def capital_downgraded(self) -> LaneSpec:
        """Fail-closed roster safety for non-capital and killed strategies.

        Applied at roster build so a config or stale artifact cannot grant
        PAPER permission to a measurement-only or killed strategy.
        ``_paper_observation`` mirrors are PAPER-mode but non-capital (they submit
        no orders), so they are left untouched. See docs/SCANNER_REVIEW_20260813."""
        if (
            self.mode is RunnerMode.PAPER
            and not self.lane_id.endswith("_paper_observation")
            and not is_capital_eligible(self.strategy_id)
        ):
            return replace(self, mode=RunnerMode.SHADOW)
        return self


@dataclass(frozen=True)
class _LaneRuntime:
    spec: LaneSpec
    session: LivePaperSession
    feed: LiveMarketFeed | RestPollingMarketFeed | SharedFeedView


# --- snapshot fan-in --------------------------------------------------------------


class _LaneSink:
    """A provider-shaped object one lane publishes to; tags + forwards."""

    def __init__(self, parent: MultiLaneProvider, lane_id: str, exchange: str) -> None:
        self._parent, self._lane_id, self._exchange = parent, lane_id, exchange

    def publish(self, snapshot: dict) -> None:
        self._parent._publish(self._lane_id, self._exchange, snapshot)


class MultiLaneProvider:
    """Holds each lane's latest snapshot. latest() returns the primary lane
    (flat, backward-compatible) with a `lanes` array appended for comparison."""

    # Lane-health audit cadence: recompute at most every 5 minutes — the
    # audit reads file tails, so it must stay off the per-snapshot hot path.
    LANE_HEALTH_INTERVAL_SECONDS = 300.0

    def __init__(
        self,
        primary_lane_id: str,
        *,
        lane_specs: list[LaneSpec] | None = None,
        journal_dir: Path | None = None,
        runtime_control: dict | None = None,
        canonical_router: CanonicalCandleRouter | None = None,
    ) -> None:
        self.primary = primary_lane_id
        self._lanes: dict[str, dict] = {}
        self._published_at: dict[str, float] = {}
        self._order: list[str] = []
        # optional lane-health audit (desired-vs-active); off unless both
        # the desired spec list and the journal dir are provided
        self._health_specs = list(lane_specs) if lane_specs is not None else None
        self._health_journal_dir = Path(journal_dir) if journal_dir is not None else None
        self._health_cache: dict | None = None
        self._health_at = 0.0
        self._runtime_control = dict(runtime_control or {})
        self._canonical_router = canonical_router
        self._specs_by_id = {spec.lane_id: spec for spec in (lane_specs or [])}
        self._recorder_latency_root = Path(
            os.environ.get("RECORDER_LATENCY_ROOT", "data/reports/recorder_latency")
        )
        self._recorder_latency_cache: list[dict] = []
        self._recorder_latency_at = 0.0

    def _recorder_latency(self) -> list[dict]:
        """Import recorder-process gauges; never infer them in lane process."""
        now = time.monotonic()
        if now - self._recorder_latency_at < 5.0:
            return list(self._recorder_latency_cache)
        self._recorder_latency_at = now
        snapshots: list[dict] = []
        try:
            for path in sorted(self._recorder_latency_root.glob("*.json")):
                payload = RecorderLatencyStore(path).load()
                if payload is not None:
                    snapshots.append(payload)
        except OSError as exc:
            logger.warning("recorder latency import failed: %s", exc)
        self._recorder_latency_cache = snapshots
        return list(snapshots)

    def _lane_health(self) -> dict | None:
        """Cached desired-vs-active audit for the snapshot (never raises).

        The auditor cross-checks the configured spec list against the lane
        journal/equity files on disk; any exception is swallowed (keeping the
        previous result) — lane health must never take down the snapshot."""
        if self._health_specs is None or self._health_journal_dir is None:
            return None
        now = time.monotonic()
        if self._health_cache is not None and (
            now - self._health_at < self.LANE_HEALTH_INTERVAL_SECONDS
        ):
            return self._health_cache
        self._health_at = now  # even on failure: don't re-fail on every snapshot
        try:
            from vnedge.runtime.lane_health import audit_lanes

            report = audit_lanes(self._health_journal_dir, desired=self._health_specs)
            self._health_cache = report.to_snapshot()
        except Exception as exc:  # noqa: BLE001 — observability must not crash lanes
            logger.warning("lane-health audit failed: %s", exc)
        return self._health_cache

    def sink(self, lane_id: str, exchange: str) -> _LaneSink:
        return _LaneSink(self, lane_id, exchange)

    def _publish(self, lane_id: str, exchange: str, snapshot: dict) -> None:
        snap = dict(snapshot)
        snap["lane_id"] = lane_id
        snap["lane_exchange"] = exchange
        if lane_id not in self._lanes:
            self._order.append(lane_id)
        self._lanes[lane_id] = snap
        self._published_at[lane_id] = time.monotonic()

    def age_seconds(self) -> float | None:
        """Age of the primary snapshot consumed by the dashboard.

        ``SnapshotProvider`` has always exposed this contract.  The multi-lane
        fan-in is the production provider, so omitting it made observability
        endpoints fail with HTTP 500 even while the runtime itself was fresh.
        """
        if not self._order:
            return None
        lane_id = self.primary if self.primary in self._published_at else self._order[0]
        published_at = self._published_at.get(lane_id)
        if published_at is None:
            return None
        return max(0.0, time.monotonic() - published_at)

    def publish_warming(self, lane_id: str, exchange: str, symbol: str) -> None:
        """(C) A placeholder published for every lane BEFORE it builds, so the
        dashboard shows the whole fleet immediately (each lane 'warming up')
        instead of a blank board until all builds finish. Overwritten by the
        lane's real snapshot as soon as it starts."""
        spec = self._specs_by_id.get(lane_id)
        self._publish(
            lane_id,
            exchange,
            {
                "mode": "warming up",
                "symbol": symbol,
                "strategy_id": spec.strategy_id if spec is not None else "",
                "equity": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "fills": 0,
                "fees_usd": 0.0,
                "risk_status": "warming",
                "feed_health": {"candles": "warming", "last_update_ms": 0.0},
                "positions": [],
                "open_orders": [],
                "session": {},
            },
        )

    def publish_error(self, lane_id: str, exchange: str, symbol: str, error: str) -> None:
        self._publish(
            lane_id,
            exchange,
            {
                "mode": "shadow (live data)",
                "symbol": symbol,
                "equity": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "fills": 0,
                "fees_usd": 0.0,
                "risk_status": "lane_error",
                "feed_health": {"candles": "error", "last_update_ms": 0.0},
                "positions": [],
                "open_orders": [],
                "recent_alerts": [
                    {
                        "severity": "critical",
                        "message": error,
                        "rule_id": "lane_error",
                    }
                ],
                "session": {"lane_error": error},
            },
        )

    def latest(self) -> dict | None:
        if not self._lanes:
            return None
        primary = self._lanes.get(self.primary) or self._lanes[self._order[0]]
        out = dict(primary)
        # canonical candle-path fields: promote from the primary lane's session
        # and stamp the REAL serving-time freshness (age since the primary lane
        # last published its snapshot). UI reads these top-level, not session.*.
        primary_session = primary.get("session") or {}
        out.setdefault("time_machine", primary_session.get("time_machine"))
        out.setdefault("latency", primary_session.get("latency"))
        out.setdefault("latency_recovery", primary_session.get("latency_recovery"))
        out["recorder_latency"] = self._recorder_latency()
        if self._canonical_router is not None:
            # Report-only transport truth. These counters never grant arm or
            # order permission; stream failures already fail subscriptions.
            out["canonical_router"] = asdict(self._canonical_router.snapshot())
        try:
            ts = primary.get("ts")
            age_ms = (
                (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds() * 1000.0
                if ts
                else None
            )
            out["snapshot_age_ms"] = round(max(0.0, age_ms), 1) if age_ms is not None else None
        except (TypeError, ValueError):
            out["snapshot_age_ms"] = None
        out["lanes"] = [
            {
                "lane_id": self._lanes[lid]["lane_id"],
                "exchange": self._lanes[lid]["lane_exchange"],
                "symbol": self._lanes[lid].get("symbol", ""),
                # per-lane mode + strategy so the dashboard lane matrix can
                # label paper vs shadow and which strategy each lane runs
                "mode": self._lanes[lid].get("mode", ""),
                "strategy_id": self._lanes[lid].get("strategy_id", "")
                or (self._specs_by_id[lid].strategy_id if lid in self._specs_by_id else ""),
                # per-lane last price + funding so the dashboard watchlist can
                # show real per-symbol quotes (one live price per lane symbol)
                "price": self._lanes[lid].get("price"),
                "funding_rate": self._lanes[lid].get("funding_rate"),
                "equity": self._lanes[lid].get("equity", 0.0),
                "realized_pnl": self._lanes[lid].get("realized_pnl", 0.0),
                "unrealized_pnl": self._lanes[lid].get("unrealized_pnl", 0.0),
                "fills": self._lanes[lid].get("fills", 0),
                "fees_usd": self._lanes[lid].get("fees_usd", 0.0),
                "risk_status": self._lanes[lid].get("risk_status", "?"),
                "last_risk_reject": self._lanes[lid].get("last_risk_reject"),
                "journal": self._lanes[lid].get("journal"),
                "feed": self._lanes[lid].get("feed_health", {}).get("candles", "?"),
                # transport + staleness so the dashboard connections panel can
                # show per-venue pipe health without new endpoints
                "feed_mode": self._lanes[lid].get("feed_health", {}).get("exchange", ""),
                "staleness_ms": self._lanes[lid].get("feed_health", {}).get("last_update_ms"),
                "positions": len(self._lanes[lid].get("positions", [])),
                # signal funnel: evaluated -> fired -> approved/submitted -> filled
                "funnel": {
                    "bars": self._lanes[lid].get("session", {}).get("bars_processed", 0),
                    "evals": self._lanes[lid].get("session", {}).get("evals", 0),
                    "live_evals": self._lanes[lid].get("session", {}).get("live_evals", 0),
                    "backfill_evals": self._lanes[lid].get("session", {}).get("backfill_evals", 0),
                    "signals": self._lanes[lid].get("session", {}).get("signals", 0),
                    "live_signals": self._lanes[lid].get("session", {}).get("live_signals", 0),
                    "backfill_signals": self._lanes[lid]
                    .get("session", {})
                    .get("backfill_signals", 0),
                    "shadow_approved": self._lanes[lid]
                    .get("session", {})
                    .get("shadow_approved", 0),
                    "shadow_rejected": self._lanes[lid]
                    .get("session", {})
                    .get("shadow_rejected", 0),
                    "risk_rejects": self._lanes[lid].get("session", {}).get("risk_rejects", 0),
                    "sizing_skips": self._lanes[lid].get("session", {}).get("sizing_skips", 0),
                    "submitted": self._lanes[lid].get("session", {}).get("orders_submitted", 0),
                    "fills": self._lanes[lid].get("fills", 0),
                },
                # virtual performance of a shadow lane's approved intents
                # (resolved with backtester semantics; observability only)
                "shadow_perf": self._lanes[lid].get("session", {}).get("shadow_perf"),
                "sizing_profile": self._lanes[lid].get("session", {}).get("sizing_profile"),
                "active_plan": self._lanes[lid].get("session", {}).get("active_plan"),
                "last_reject_reason": self._lanes[lid].get("session", {}).get("last_reject_reason"),
                "observation_class": (
                    "shadow_observe"
                    if self._specs_by_id.get(lid) is not None
                    and self._specs_by_id[lid].lane_id.startswith("shadow_observe_")
                    else "measurement"
                    if self._specs_by_id.get(lid) is not None
                    and self._specs_by_id[lid].strategy_id == "measurement_only_v1"
                    else None
                ),
                # latest strategy evaluation (features + thresholds) so the
                # lane matrix can explain WHY a lane is waiting/near trigger
                "last_eval": self._lanes[lid].get("session", {}).get("last_eval"),
                # cumulative funnel survives restarts (LaneFunnelStore); these
                # two let the dashboard show "last fired 2d ago · 4h bars"
                "last_fired_ts": self._lanes[lid].get("session", {}).get("last_fired_ts"),
                "last_quote_signal": self._lanes[lid]
                .get("session", {})
                .get("last_quote_signal"),
                "timeframe": self._lanes[lid].get("session", {}).get("timeframe"),
                # pipeline latency: bar_close_processing_ms (close -> dequeue)
                # + decision_lag_ms (bar -> signal), each {last,p50,p95,max,n}
                "latency": self._lanes[lid].get("session", {}).get("latency"),
                "latency_recovery": self._lanes[lid].get("session", {}).get("latency_recovery"),
                # feed-continuity guard: non-null ⇒ lane is reduce-only (gap/stall)
                "degraded": self._lanes[lid].get("session", {}).get("degraded"),
                "gapped_candles": self._lanes[lid].get("session", {}).get("gapped_candles", 0),
                # multi-TF forming+closed awareness (read-only observability)
                "time_machine": self._lanes[lid].get("session", {}).get("time_machine"),
                # candle-path arm-gate: cumulative skips + CURRENT block reason
                "decision_skips": self._lanes[lid].get("session", {}).get("decision_skips"),
                "arm_blocked": self._lanes[lid].get("session", {}).get("arm_blocked"),
                # D-lite overlays (observe-only): cost world + regime + plan preview
                "cost_profile": self._lanes[lid].get("session", {}).get("cost_profile"),
                "cost_profile_source": self._lanes[lid]
                .get("session", {})
                .get("cost_profile_source"),
                "data_exchange": self._lanes[lid].get("session", {}).get("data_exchange"),
                "execution_cost_exchange": self._lanes[lid]
                .get("session", {})
                .get("execution_cost_exchange"),
                "scanner_cost_hypothesis": self._lanes[lid]
                .get("session", {})
                .get("scanner_cost_hypothesis"),
                "runtime_contract": self._lanes[lid].get("session", {}).get("runtime_contract"),
                "regime": self._lanes[lid].get("session", {}).get("regime"),
                "regime_would_block": self._lanes[lid].get("session", {}).get("regime_would_block"),
                "plan_overlay": self._lanes[lid].get("session", {}).get("plan_overlay"),
                "plan_gate_rejects": self._lanes[lid].get("session", {}).get("plan_gate_rejects"),
                # per-lane drawdown vs trial limit + trial scorecard (observe-only)
                "peak_equity": self._lanes[lid].get("session", {}).get("peak_equity"),
                "drawdown_pct": self._lanes[lid].get("session", {}).get("drawdown_pct"),
                "dd_limit_pct": self._lanes[lid].get("session", {}).get("dd_limit_pct"),
                "trial_scorecard": self._lanes[lid].get("session", {}).get("trial_scorecard"),
                "daily_factory": self._lanes[lid].get("session", {}).get("daily_factory"),
                "trade_log": (self._lanes[lid].get("session", {}).get("trade_log") or [])[-10:],
                "trade_compatibility": _lane_trade_compatibility(self._lanes[lid]),
            }
            for lid in self._order
            if lid in self._lanes
        ]
        out["fleet"] = _fleet_aggregate(out["lanes"])
        health = self._lane_health()
        if health is not None:
            out["lane_health"] = health
        if self._runtime_control:
            out["runtime_control"] = dict(self._runtime_control)
        annotate(out)  # server-computed chips + per-lane bands (one source for both UIs)
        # A single inf/nan anywhere (e.g. a degenerate quote's spread_bps) makes
        # the whole snapshot fail JSON serialization — Starlette's JSONResponse
        # and websocket.send_json both use allow_nan=False — which 500s /state
        # and drops /ws, freezing the dashboard. Scrub non-finite floats to null.
        return _json_safe(out)


# --- lane construction ------------------------------------------------------------


def _lane_trade_compatibility(snapshot: dict) -> dict:
    """Operator-facing state: whether this lane is clean enough to discuss promotion.

    This does not grant permissions and never bypasses the gateway. It only
    prevents exploratory/negative shadow evidence from looking like a healthy
    paper/live candidate in the cockpit.
    """
    mode = str(snapshot.get("mode", "")).lower()
    risk_status = str(snapshot.get("risk_status", ""))
    feed = snapshot.get("feed_health", {})
    session = snapshot.get("session", {})
    shadow_perf = session.get("shadow_perf") or {}
    is_shadow = "shadow" in mode
    virtual_trades = int(shadow_perf.get("virtual_trades") or 0)
    virtual_net = float(shadow_perf.get("net_usd") or 0.0)
    if risk_status != "ok":
        state, reason = "BLOCKED", f"risk status {risk_status}"
    elif feed.get("candles") != "ok":
        state, reason = "BLOCKED", f"feed {feed.get('candles', '?')}"
    elif is_shadow and virtual_trades > 0 and virtual_net < 0:
        state, reason = "SHADOW_PROBATION", f"virtual net {virtual_net:+.2f} USD"
    elif is_shadow:
        state, reason = "SHADOW_ONLY", "observability only; no fills/live orders"
    else:
        state, reason = "PAPER_COMPATIBLE", "simulated fills through live-data path"
    return {
        "state": state,
        "reason": reason,
        "real_orders_allowed": False,
        "gateway_required": True,
        "journal_required": True,
    }


def _json_safe(obj):
    """Recursively replace non-finite floats (inf/-inf/nan) with None.

    Starlette serializes JSON with allow_nan=False, so one inf/nan anywhere in
    the published snapshot raises ValueError and 500s /state (or drops /ws) —
    which silently freezes the dashboard on stale data. Null is always safe.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {key: _json_safe(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(value) for value in obj]
    return obj


def _fleet_aggregate(lanes: list[dict]) -> dict:
    """Portfolio-wide truth across the ACTIVELY-RUNNING lanes.

    The flat snapshot's ``equity`` is the primary lane only — a single lane's
    NAV must never masquerade as the whole bot (a green primary lane hiding a
    red fleet is exactly the honesty bug this fixes). This sums every running
    lane so the headline is the real book, and splits paper (deploys paper
    capital) from shadow (virtual/observation-only) so neither dilutes the
    other. Error lanes are excluded — a crashed lane is not a $0 account.
    """
    eq = start = realized = unrealized = fees = 0.0
    # paper_* = only the lanes that actually deploy capital. Shadow lanes hold a
    # static nominal account that never moves, so folding them into the headline
    # return dilutes it to ~0 and hides how the traded book is really doing.
    paper_eq = paper_start = 0.0
    paper_n = shadow_n = profitable = losing = 0
    shadow_net = 0.0
    shadow_open_net = 0.0
    shadow_open_positions = 0
    shadow_trades = 0
    counted = 0
    for lane in lanes:
        if str(lane.get("risk_status") or "") == "lane_error":
            continue
        counted += 1
        e = float(lane.get("equity") or 0.0)
        r = float(lane.get("realized_pnl") or 0.0)
        u = float(lane.get("unrealized_pnl") or 0.0)
        f = float(lane.get("fees_usd") or 0.0)
        eq += e
        realized += r
        unrealized += u
        fees += f
        lane_start = e - r - u  # starting = equity - realized - unrealized
        start += lane_start
        if "shadow" in str(lane.get("mode") or "").lower():
            shadow_n += 1
            sp = lane.get("shadow_perf") or {}
            shadow_net += float(sp.get("net_usd") or 0.0)
            shadow_open_net += float(sp.get("open_unrealized_net_usd") or 0.0)
            shadow_open_positions += int(sp.get("open_intents") or 0)
            shadow_trades += int(sp.get("virtual_trades") or 0)
        else:
            paper_n += 1
            paper_eq += e
            paper_start += lane_start
        if r > 1e-9:
            profitable += 1
        elif r < -1e-9:
            losing += 1
    ret_pct = ((eq - start) / start * 100.0) if start > 1e-9 else 0.0
    paper_ret_pct = ((paper_eq - paper_start) / paper_start * 100.0) if paper_start > 1e-9 else 0.0
    return {
        "lanes": counted,
        "equity": round(eq, 2),
        "starting_equity": round(start, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "fees_usd": round(fees, 2),
        "return_pct": round(ret_pct, 3),
        "paper_lanes": paper_n,
        "shadow_lanes": shadow_n,
        # the traded book, undiluted by static shadow accounts
        "paper_equity": round(paper_eq, 2),
        "paper_starting_equity": round(paper_start, 2),
        "paper_return_pct": round(paper_ret_pct, 3),
        "shadow_virtual_net_usd": round(shadow_net, 2),
        "shadow_open_unrealized_net_usd": round(shadow_open_net, 2),
        "shadow_total_net_usd": round(shadow_net + shadow_open_net, 2),
        "shadow_open_positions": shadow_open_positions,
        "shadow_virtual_trades": shadow_trades,
        "profitable_lanes": profitable,
        "losing_lanes": losing,
    }


_FUNDING_HISTORY_REQUIRED = {"funding_mean_reversion_v1"}

# How far back the native Delta FUNDING: candle backfill reaches. 30 days of
# hourly prints covers the 240-bar percentile window (~10d) with room to
# spare, so a freshly built lane is warm on every bar of the candle warmup.
_DELTA_FUNDING_BACKFILL_DAYS = 30


def _requires_funding_history(strategy_id: str) -> bool:
    return strategy_id in _FUNDING_HISTORY_REQUIRED


async def _delta_funding_seed(spec: LaneSpec, fallback: pd.DataFrame) -> pd.DataFrame:
    """Backfill a funding seed from Delta's native ``FUNDING:`` candle API.

    Delta has no CCXT funding history, but its candle endpoint serves settled
    funding prints under the ``FUNDING:`` symbol prefix — seeding the
    percentile window at build time instead of a ~10-day live warmup.

    Failure posture: on ANY error (or an empty response) return ``fallback``
    — today's behaviour: empty seed, live accumulation behind the warmup
    mask. The backfill must never crash lane build.
    """
    from vnedge.data.delta_native_history import (
        DELTA_NATIVE_EXCHANGE_IDS,
        fetch_delta_funding_history,
    )

    if spec.exchange not in DELTA_NATIVE_EXCHANGE_IDS:
        return fallback
    try:
        seed = await fetch_delta_funding_history(spec.symbol, days=_DELTA_FUNDING_BACKFILL_DAYS)
    except Exception as exc:  # noqa: BLE001 — backfill is strictly best-effort
        logger.warning(
            "%s %s: native funding backfill failed (%s); lane keeps the "
            "live-accumulation warmup path",
            spec.exchange,
            spec.symbol,
            exc,
        )
        return fallback
    if seed.empty:
        logger.warning(
            "%s %s: native funding backfill returned no data; lane keeps the "
            "live-accumulation warmup path",
            spec.exchange,
            spec.symbol,
        )
        return fallback
    logger.info(
        "%s %s: seeded %d settled funding prints (%s -> %s) from the native "
        "FUNDING: candle API — percentile window warm from the start",
        spec.exchange,
        spec.symbol,
        len(seed),
        seed["timestamp"].iloc[0],
        seed["timestamp"].iloc[-1],
    )
    return seed


def _build_strategy(spec: LaneSpec, seed_funding, feed, *, funding_store_path=None) -> BaseStrategy:
    """Construct the live strategy a lane runs, keyed by strategy_id."""
    params = spec.strategy_params or {}
    if spec.strategy_id == "signal_arbiter_v1":
        return _build_signal_arbiter_strategy(
            spec, seed_funding, feed, funding_store_path=funding_store_path
        )
    return _build_single_strategy(
        spec.strategy_id,
        params,
        seed_funding,
        feed,
        funding_store_path=funding_store_path,
    )


def _build_single_strategy(
    strategy_id: str,
    params: dict,
    seed_funding,
    feed,
    *,
    funding_store_path=None,
) -> BaseStrategy:
    """Construct one strategy implementation for a live-data lane."""
    if strategy_id == "funding_mean_reversion_v1":
        # No CCXT funding history (Delta) => the persistent accumulator owns
        # the series: seeded from the native FUNDING: candle backfill when
        # available (warm immediately), building live behind the warmup mask
        # otherwise; either way new prints keep appending to the fsync'd
        # store. Venues WITH history keep the proven REST-seeded path
        # unchanged (build_lane passes funding_store_path=None there).
        if funding_store_path is not None:
            from vnedge.runtime.funding_accumulator import LivePersistentFundingMR

            return LivePersistentFundingMR(
                seed_funding, feed, store_path=funding_store_path, **params
            )
        # needs the funding stream augmented live off the feed
        return LiveFundingMR(seed_funding, feed, **params)
    if strategy_id == "measurement_only_v1":
        return MeasurementOnly(seed_funding, **params)
    if strategy_id == StructureBos1H.strategy_id:
        if params:
            raise ValueError("structure_bos_1h parameters are frozen; configure a new strategy ID")
        return StructureBos1H(seed_funding, allow_price_only_live=False)
    if strategy_id == StructureBos15mTriggerV2.strategy_id:
        if params:
            raise ValueError(
                "structure_bos_15m_trigger_v2 parameters are frozen; configure a new strategy ID"
            )
        return StructureBos15mTriggerV2(seed_funding)
    if strategy_id == StructureBos15mTriggerV3.strategy_id:
        if params:
            raise ValueError(
                "structure_bos_15m_trigger_v3 parameters are frozen; configure a new strategy ID"
            )
        return StructureBos15mTriggerV3(seed_funding)
    if strategy_id == FeeWallMomentumObserver.strategy_id:
        if params:
            raise ValueError(
                "fee_wall_momentum_observer_v1 parameters are frozen; configure a new strategy ID"
            )
        return FeeWallMomentumObserver(seed_funding)
    if strategy_id == SqueezeExpansionBreakout.strategy_id:
        if params:
            raise ValueError(
                "squeeze_expansion_breakout_v2 parameters are frozen; configure a new strategy ID"
            )
        return SqueezeExpansionBreakout(seed_funding)
    if strategy_id == SqueezeExpansionBreakoutV3.strategy_id:
        if params:
            raise ValueError(
                "squeeze_expansion_breakout_v3 parameters are frozen; configure a new strategy ID"
            )
        return SqueezeExpansionBreakoutV3(seed_funding)
    if strategy_id == SqueezeExpansionBreakoutV4.strategy_id:
        if params:
            raise ValueError(
                "squeeze_expansion_breakout_v4 parameters are frozen; configure a new strategy ID"
            )
        return SqueezeExpansionBreakoutV4(seed_funding)
    if strategy_id == RangeExpansionObserver.strategy_id:
        if params:
            raise ValueError(
                "range_expansion_observer_v1 parameters are frozen; configure a new strategy ID"
            )
        return RangeExpansionObserver(seed_funding)
    if strategy_id == RangeExpansionObserverV2.strategy_id:
        if params:
            raise ValueError(
                "range_expansion_observer_v2 parameters are frozen; configure a new strategy ID"
            )
        return RangeExpansionObserverV2(seed_funding)
    if strategy_id == RangeExpansionObserverV3.strategy_id:
        if params:
            raise ValueError(
                "range_expansion_observer_v3 parameters are frozen; configure a new strategy ID"
            )
        return RangeExpansionObserverV3(seed_funding)
    if strategy_id == RangeExpansionObserverV4.strategy_id:
        if params:
            raise ValueError(
                "range_expansion_observer_v4 parameters are frozen; configure a new strategy ID"
            )
        return RangeExpansionObserverV4(seed_funding)
    for scanner_class in NEW_RESEARCH_SCANNERS:
        if strategy_id == scanner_class.strategy_id:
            if params:
                raise ValueError(
                    f"{strategy_id} parameters are frozen; configure a new strategy ID"
                )
            try:
                return scanner_class(seed_funding)
            except TypeError:
                return scanner_class()
    for realtime_scanner_class in REALTIME_SCANNERS:
        if strategy_id == realtime_scanner_class.strategy_id:
            if params:
                raise ValueError(
                    f"{strategy_id} parameters are frozen; configure a new strategy ID"
                )
            return realtime_scanner_class(seed_funding)
    if strategy_id == HtfRegimeContinuation15mV1.strategy_id:
        if params:
            raise ValueError(
                f"{strategy_id} parameters are frozen; configure a new strategy ID"
            )
        return HtfRegimeContinuation15mV1(seed_funding)
    if strategy_id == HtfRegimeContinuation15mV2.strategy_id:
        if params:
            raise ValueError(
                f"{strategy_id} parameters are frozen; configure a new strategy ID"
            )
        return HtfRegimeContinuation15mV2(seed_funding)
    if strategy_id == "trend_continuation_v1":
        # candle-only; funding is a mild static filter (fine for a shadow lane)
        return TrendContinuation(seed_funding, **params)
    if strategy_id == "crypto_trend_atr_margin_v1":
        return CryptoTrendAtrMargin(seed_funding, **params)
    if strategy_id == "volatility_expansion_breakout_v1":
        return VolatilityExpansionBreakout(seed_funding, **params)
    if strategy_id == "panic_reversal_v1":
        return PanicReversal(seed_funding, **params)
    if strategy_id == "funding_squeeze_continuation_v1":
        return FundingSqueezeContinuation(seed_funding, **params)
    raise ValueError(f"unsupported lane strategy_id: {strategy_id!r}")


def _build_signal_arbiter_strategy(
    spec: LaneSpec, seed_funding, feed, *, funding_store_path=None
) -> BaseStrategy:
    params = spec.strategy_params or {}
    children = params.get("strategies") or params.get("children")
    if not isinstance(children, list) or not children:
        raise ValueError("signal_arbiter_v1 requires a non-empty strategies list")

    strategies: list[BaseStrategy] = []
    candidate_defaults: dict[str, dict] = {}
    edge_keys = {
        "expected_edge_bps",
        "expected_cost_bps",
        "profit_factor",
        "confidence",
        "route",
        "planned_notional_usd",
        "metadata",
    }

    for index, child in enumerate(children):
        if not isinstance(child, dict):
            raise TypeError("signal_arbiter_v1 child entries must be objects")
        child_strategy_id = str(child.get("strategy_id", ""))
        if not child_strategy_id:
            raise ValueError("signal_arbiter_v1 child missing strategy_id")
        child_params = child.get("params", {})
        if not isinstance(child_params, dict):
            raise TypeError("signal_arbiter_v1 child params must be an object")

        strategies.append(
            _build_single_strategy(
                child_strategy_id,
                child_params,
                seed_funding,
                feed,
                funding_store_path=funding_store_path,
            )
        )
        default_source_id = f"{child_strategy_id}#{index + 1}"
        source_id = str(child.get("source_id", default_source_id))
        candidate_defaults[default_source_id] = {"source_id": source_id}
        candidate_defaults[source_id] = {key: child[key] for key in edge_keys if key in child}

    arbiter_params = params.get("arbiter", {})
    if not isinstance(arbiter_params, dict):
        raise TypeError("signal_arbiter_v1 arbiter config must be an object")
    return CompositeSignalStrategy(
        strategies,
        SignalArbiter(ArbiterConfig(**arbiter_params)),
        symbol=spec.symbol,
        candidate_defaults=candidate_defaults,
        strategy_id=spec.strategy_id,
    )


def _lane_risk_config(spec: LaneSpec, environ: Mapping[str, str] = os.environ) -> RiskConfig:
    """Per-lane risk config.

    Default is the LOCKED risk-based model (size from risk, halt on) — unchanged
    for every lane and, structurally, for the live path (multi-lane is
    paper/shadow only; the live trader builds its own config elsewhere).

    Opt-in PAPER-ONLY aggressive profile (env ``MULTI_LANE_FIXED_MARGIN`` > 0):
    fixed isolated-margin sizing (``$margin`` per trade, up to
    ``MULTI_LANE_FIXED_MARGIN_LEVERAGE`` — hard-capped at 30x, with leverage
    auto-reduced per-trade so the stop always fires before liquidation, so max
    loss per trade <= the margin) and the daily-loss halt off by default (the
    operator asked to remove it while paper). Reversible: unset the env var to
    return to risk-based sizing with the halt on.
    """
    margin_raw = environ.get("MULTI_LANE_FIXED_MARGIN", "").strip()
    margin_usd = 0.0
    if margin_raw:
        try:
            margin_usd = float(margin_raw)
        except ValueError:
            margin_usd = 0.0
    if margin_usd > 0:
        try:
            target_lev = int(float(environ.get("MULTI_LANE_FIXED_MARGIN_LEVERAGE", "30")))
        except ValueError:
            target_lev = 30
        target_lev = max(1, min(target_lev, ABSOLUTE_MAX_LEVERAGE))  # never above the hard cap
        halt_on = environ.get("MULTI_LANE_DAILY_LOSS_HALT", "0").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        cap = max(500.0, margin_usd * target_lev * 1.1)  # headroom for one full position
        return RiskConfig(
            max_daily_loss_usd=spec.daily_loss_usd,
            max_daily_loss_pct=2.0,
            daily_loss_halt_enabled=halt_on,
            fixed_margin_usd=margin_usd,
            max_leverage_per_position=target_lev,
            acknowledge_high_leverage=True,
            max_effective_account_leverage=10.0,
            max_exposure_per_symbol_usd=cap,
            max_total_exposure_usd=cap,
            max_open_positions=1,
        )
    return RiskConfig(max_daily_loss_usd=spec.daily_loss_usd, max_daily_loss_pct=2.0)


def _env_bool(environ: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = environ.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _lane_daily_factory_config(
    spec: LaneSpec, environ: Mapping[str, str] = os.environ
) -> DailySignalFactoryConfig:
    """Daily signal-factory policy for multi-lane paper/shadow.

    Global env keys apply to every lane; strategy-specific keys use the
    normalized strategy id, e.g.
    ``MULTI_LANE_DAILY_FACTORY_DAILY_SCALPER_PACK_V1_ENABLED=1``.
    """
    suffix = "".join(ch if ch.isalnum() else "_" for ch in spec.strategy_id.upper())

    def pick(name: str, default: str | None = None) -> str | None:
        specific = environ.get(f"MULTI_LANE_DAILY_FACTORY_{suffix}_{name}")
        if specific is not None:
            return specific
        return environ.get(f"MULTI_LANE_DAILY_FACTORY_{name}", default)

    def pick_bool(name: str, default: bool) -> bool:
        raw = pick(name)
        if raw is None or str(raw).strip() == "":
            return default
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def pick_int(name: str, default: int) -> int:
        raw = pick(name)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return int(float(str(raw).strip()))
        except ValueError:
            return default

    def pick_float(name: str, default: float) -> float:
        raw = pick(name)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return float(str(raw).strip())
        except ValueError:
            return default

    enabled_default = _env_bool(environ, "MULTI_LANE_DAILY_FACTORY_ENABLED", False)
    timezone = str(pick("TIMEZONE", "UTC") or "UTC")
    return DailySignalFactoryConfig(
        enabled=pick_bool("ENABLED", enabled_default),
        session_timezone=timezone,
        entry_cutoff_minute=pick_int("ENTRY_CUTOFF_MINUTE", 22 * 60 + 30),
        force_flatten_minute=pick_int("FORCE_FLATTEN_MINUTE", 23 * 60 + 55),
        max_entries_per_day=pick_int("MAX_ENTRIES_PER_DAY", 3),
        daily_profit_target_usd=pick_float("DAILY_PROFIT_TARGET_USD", 0.0),
        cancel_resting_entries_at_cutoff=pick_bool("CANCEL_RESTING_ENTRIES", True),
        flatten_open_positions=pick_bool("FLATTEN_OPEN_POSITIONS", True),
    )


_TF_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "1d": 86_400_000,
}
_CANDLE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


def _canonical_candle_frame(
    store: CandleParquetStore,
    symbol: str,
    timeframe: str,
    *,
    since_ms: int,
    until_ms: int,
) -> pd.DataFrame:
    """Read the exact-volume lake slice used to enrich scanner warm-up.

    A partial lake is still useful: prices remain sourced from the validated
    CCXT seed outside its coverage, while exact fields are populated only on
    matching canonical rows. Strict scanners can then fail closed until their
    own exact-data window is complete instead of treating a missing field as 0.
    """
    candles = [
        candle
        for candle in store.read(symbol, timeframe)
        if since_ms <= int(candle.open_time.timestamp() * 1000) < until_ms
    ]
    if not candles:
        return pd.DataFrame()
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([candle.open_time for candle in candles], utc=True),
            "open": [float(candle.open) for candle in candles],
            "high": [float(candle.high) for candle in candles],
            "low": [float(candle.low) for candle in candles],
            "close": [float(candle.close) for candle in candles],
            "volume": [float(candle.volume) for candle in candles],
            "quote_volume": [float(candle.quote_volume) for candle in candles],
            "trade_count": [candle.trade_count for candle in candles],
            "taker_buy_volume": [float(candle.taker_buy_volume) for candle in candles],
            "vwap": [
                float(candle.vwap) if candle.vwap is not None else float("nan")
                for candle in candles
            ],
            "data_quality": "ok",
            "is_closed": True,
            "timeframe": timeframe,
            "symbol": symbol,
            "candle_source": "canonical_tick_lake",
        }
    )


def _overlay_canonical_history(
    history: pd.DataFrame,
    canonical: pd.DataFrame,
    *,
    allow_validated_exchange_ohlcv: bool = False,
) -> pd.DataFrame:
    """Overlay exact trade-derived rows onto one validated venue history.

    Exchange OHLCV is non-armable by default because it cannot satisfy the
    quote-volume/trade-count contract used by range, AVWAP, and flow lanes.
    The sole opt-in is a strategy whose registered contract explicitly names
    official price-only OHLC as its structure source.
    """
    default_quality = "ok" if allow_validated_exchange_ohlcv else "gap"
    default_source = (
        "exchange_ohlcv_validated"
        if allow_validated_exchange_ohlcv
        else "exchange_ohlcv"
    )
    if canonical.empty:
        out = history.copy()
        # Absence of the canonical lake must not be more permissive than a
        # partially populated lake. Every exchange-only row is explicit and
        # non-armable until exact trade-derived truth is available.
        out["data_quality"] = default_quality
        out["is_closed"] = True
        out["candle_source"] = default_source
        return out
    out = history.copy()
    out["data_quality"] = default_quality
    out["is_closed"] = True
    out["candle_source"] = default_source
    out = out.set_index("timestamp")
    exact = canonical.copy().set_index("timestamp")
    for name in exact.columns:
        if name not in out:
            out[name] = pd.NA
        overlap = out.index.intersection(exact.index)
        if len(overlap):
            out.loc[overlap, name] = exact.loc[overlap, name]
    out["candle_source"] = out["candle_source"].fillna(default_source)
    return out.reset_index().sort_values("timestamp").reset_index(drop=True)


_VALIDATED_EXCHANGE_OHLCV_STRATEGIES = frozenset(
    {HtfRegimeContinuation15mV2.strategy_id}
)
_CONTEXT_WARMUP_BARS = 800


def _allows_validated_exchange_ohlcv(spec: LaneSpec) -> bool:
    """Whether this exact registered ID may arm from official price-only OHLC."""
    return spec.strategy_id in _VALIDATED_EXCHANGE_OHLCV_STRATEGIES


def _warmup_since_for_timeframe(
    timeframe: str,
    until_ms: int,
    *,
    bars: int,
) -> int:
    """Return a close-boundary-aligned lookback for one independent clock."""
    timeframe_ms = _timeframe_ms(timeframe)
    boundary = (until_ms // timeframe_ms) * timeframe_ms
    return boundary - max(1, int(bars)) * timeframe_ms


def _canonical_runtime_store(
    spec: LaneSpec,
    store: CandleParquetStore,
) -> CandleParquetStore | None:
    """Select whether live decisions must wait for a canonical tick candle.

    Binance owns the production tick recorder and exact candle ladder, so all
    Binance lanes consume it. Non-Binance measurement lanes are deliberately
    read-only and use their validated public OHLCV feed; attaching an empty
    Bybit/Delta candle partition made those non-arming lanes emit permanent
    ``canonical_bar_timeout`` failures. A future non-measurement lane on either
    venue remains fail-closed by receiving the empty canonical store until a
    venue recorder is explicitly built.
    """
    if spec.exchange != "binanceusdm" and spec.strategy_id == "measurement_only_v1":
        return None
    return store


def _timeframe_ms(timeframe: str) -> int:
    return _TF_MS.get(str(timeframe).strip(), 3_600_000)  # default 1h


def _warmup_bars(environ: Mapping[str, str] = os.environ) -> int:
    """Warmup lookback in BARS (covers the longest indicator window with room);
    right-sizes the fetch per timeframe instead of a fixed 450h."""
    try:
        return max(50, int(environ.get("MULTI_LANE_WARMUP_BARS", "500")))
    except ValueError:
        return 500


_FIXED_STRATEGY_WARMUPS: dict[str, int] = {
    MeasurementOnly.strategy_id: MeasurementOnly.warmup_bars,
    FeeWallMomentumObserver.strategy_id: FeeWallMomentumObserver.warmup_bars,
    SqueezeExpansionBreakout.strategy_id: SqueezeExpansionBreakout.warmup_bars,
    SqueezeExpansionBreakoutV3.strategy_id: SqueezeExpansionBreakoutV3.warmup_bars,
    SqueezeExpansionBreakoutV4.strategy_id: SqueezeExpansionBreakoutV4.warmup_bars,
    RangeExpansionObserver.strategy_id: RangeExpansionObserver.warmup_bars,
    RangeExpansionObserverV2.strategy_id: RangeExpansionObserverV2.warmup_bars,
    RangeExpansionObserverV3.strategy_id: RangeExpansionObserverV3.warmup_bars,
    RangeExpansionObserverV4.strategy_id: RangeExpansionObserverV4.warmup_bars,
    StructureBos1H.strategy_id: StructureBos1H.warmup_bars,
    StructureBos15mTriggerV2.strategy_id: StructureBos15mTriggerV2.warmup_bars,
    StructureBos15mTriggerV3.strategy_id: StructureBos15mTriggerV3.warmup_bars,
    **{strategy.strategy_id: strategy.warmup_bars for strategy in NEW_RESEARCH_SCANNERS},
    **{strategy.strategy_id: strategy.warmup_bars for strategy in REALTIME_SCANNERS},
    HtfRegimeContinuation15mV1.strategy_id: HtfRegimeContinuation15mV1.warmup_bars,
    HtfRegimeContinuation15mV2.strategy_id: HtfRegimeContinuation15mV2.warmup_bars,
}


def _strategy_warmup_requirement(spec: LaneSpec) -> int:
    """Return the known causal feature requirement for one lane."""
    required = int(_FIXED_STRATEGY_WARMUPS.get(spec.strategy_id, 0))
    if spec.strategy_id != "signal_arbiter_v1":
        return required
    params = spec.strategy_params or {}
    children = params.get("strategies") or params.get("children") or []
    if not isinstance(children, list):
        return required
    return max(
        [required]
        + [
            int(_FIXED_STRATEGY_WARMUPS.get(str(child.get("strategy_id")), 0))
            for child in children
            if isinstance(child, Mapping)
        ]
    )


def _required_warmup_bars(spec: LaneSpec, environ: Mapping[str, str] = os.environ) -> int:
    """Return a lane-specific history target with one evaluable bar of room.

    The old global 500-bar target was smaller than squeeze v3's frozen 2,065
    bar feature window.  That lane therefore restarted with a healthy feed but
    could not evaluate for another ~5.4 days.  Fixed strategies expose their
    causal requirement as ``warmup_bars``; an arbiter inherits the largest
    fixed child requirement.  Dynamic legacy strategies retain the conservative
    operator baseline until their instance is built.
    """
    return max(_warmup_bars(environ), _strategy_warmup_requirement(spec) + 1)


#: Retry policy for a lane's warmup build — transient venue network/rate-limit
#: errors are common when the whole fleet warms up at once; a couple of backed-off
#: retries recover them instead of dropping the lane for the whole session.
_LANE_BUILD_RETRIES = 3
_LANE_BUILD_BACKOFF_S = 1.5


def _lane_build_concurrency(environ: Mapping[str, str] = os.environ) -> int:
    """How many lanes may warm up (hit the venue REST APIs) at once.

    Building all ~50 lanes simultaneously bursts each venue's instrument/candle
    endpoints and rate-limits a chunk of them into build failures. A small cap
    spreads the load so the fleet reliably comes up whole; env-tunable."""
    try:
        return max(1, int(environ.get("MULTI_LANE_BUILD_CONCURRENCY", "6")))
    except ValueError:
        return 6


async def _retry_transient(factory, *, retries: int, backoff_s: float, label: str):
    """Await ``factory()`` with bounded retries on transient venue errors.

    ccxt ``NetworkError`` (timeouts, rate limits, exchange-not-available) during
    warmup is transient — back off and retry rather than losing the lane for the
    session. Non-network errors (bad symbol, etc.) raise immediately."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return await factory()
        except NetworkError as exc:
            last = exc
            if attempt + 1 < retries:
                logger.warning(
                    "transient venue error building %s (attempt %d/%d): %s; retrying",
                    label,
                    attempt + 1,
                    retries,
                    exc,
                )
                await asyncio.sleep(backoff_s * (attempt + 1))
    assert last is not None
    raise last


def _load_candle_cache(cache_path: Path):
    """A lane's persisted candle window, or None if missing/unreadable/malformed.
    A moved or corrupt cache is never trusted — warmup refetches instead."""
    if not cache_path.exists():
        return None
    try:
        frame = pd.read_parquet(cache_path)
    except Exception as exc:  # noqa: BLE001 — a bad cache must never crash a lane
        logger.warning("candle cache unreadable at %s: %s — refetching", cache_path, exc)
        return None
    if frame.empty or not set(_CANDLE_COLUMNS).issubset(frame.columns):
        return None
    return frame


def _save_candle_cache(cache_path: Path, history) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".parquet.tmp")
        history.to_parquet(tmp, index=False)
        tmp.replace(cache_path)
    except Exception as exc:  # noqa: BLE001 — caching is best-effort, never fatal
        logger.warning("candle cache save failed for %s: %s", cache_path, exc)


async def _warmup_candles(rest, spec: LaneSpec, cache_path: Path, since: int, until: int):
    """Return ``[since, until]`` using the cache plus only missing ranges.

    A normal restart fetches only the tail after the newest cached candle.  If
    a strategy later requires a longer history (for example squeeze v3 needs
    2,065 bars while the old cache held 500), fetch only the missing *prefix*
    and merge it with the cache.  Internal holes are likewise fetched as
    bounded ranges.  A non-overlapping or unreadable cache requires one full
    requested-window fetch; no synthetic candles are ever invented.
    """
    cached = _load_candle_cache(cache_path)
    if cached is not None:
        cached = (
            cached.drop_duplicates(subset="timestamp", keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        tf_ms = _timeframe_ms(spec.timeframe)
        opens = [int(pd.Timestamp(value).timestamp() * 1000) for value in cached["timestamp"]]
        first_ms, last_ms = opens[0], opens[-1]
        if last_ms >= since and first_ms < until:  # requested window overlaps cache
            missing: list[tuple[int, int]] = []
            if first_ms > since:
                missing.append((since, first_ms))
            for previous, current in pairwise(opens):
                next_open = previous + tf_ms
                if current > next_open and next_open < until and current > since:
                    missing.append((max(next_open, since), min(current, until)))
            next_open_ms = last_ms + tf_ms
            if next_open_ms < until:
                missing.append((max(next_open_ms, since), until))

            frames = [cached]
            for gap_since, gap_until in missing:
                if gap_since >= gap_until:
                    continue
                gap = normalize_candles(
                    await rest.fetch_candles(spec.symbol, spec.timeframe, gap_since, gap_until)
                )
                if not gap.empty:
                    frames.append(gap)
            frame = (
                pd.concat(frames, ignore_index=True)
                .drop_duplicates(subset="timestamp", keep="last")
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
            cutoff = pd.to_datetime(since, unit="ms", utc=True)
            frame = frame[frame["timestamp"] >= cutoff].reset_index(drop=True)
            frame = _closed_validated_warmup(frame, spec, until)
            _save_candle_cache(cache_path, frame)
            logger.info(
                "lane %s warmup: cache + %d missing range(s) (%d bars)",
                spec.lane_id,
                len(missing),
                len(frame),
            )
            return frame
    history = normalize_candles(await rest.fetch_candles(spec.symbol, spec.timeframe, since, until))
    history = _closed_validated_warmup(history, spec, until)
    _save_candle_cache(cache_path, history)
    return history


def _closed_validated_warmup(
    frame: pd.DataFrame,
    spec: LaneSpec,
    until_ms: int,
) -> pd.DataFrame:
    """Remove the exchange's forming tail and enforce the quality boundary."""
    timeframe_ms = _timeframe_ms(spec.timeframe)
    cutoff = pd.to_datetime(until_ms, unit="ms", utc=True)
    closed = frame[
        frame["timestamp"] + pd.to_timedelta(timeframe_ms, unit="ms") <= cutoff
    ].reset_index(drop=True)
    report = validate_candles(
        closed,
        spec.timeframe,
        allow_gaps=False,
        dataset=f"runtime_warmup/{spec.exchange}/{spec.symbol}/{spec.timeframe}",
    )
    if not report.passed:
        raise RuntimeError(report.summary)
    return closed


async def build_lane(
    spec: LaneSpec,
    provider: MultiLaneProvider,
    journal_dir: Path,
    shadow_portfolio: ShadowPortfolioGate | None = None,
    canonical_router: CanonicalCandleRouter | None = None,
    canonical_router_exchanges: frozenset[str] = frozenset(),
    canonical_arm_health: Callable[[], str | None] | None = None,
) -> _LaneRuntime:
    """Seed warmup history + build an isolated LivePaperSession for one venue."""
    # (A) Bars-based warmup: the operator baseline or the strategy's causal
    # feature requirement, whichever is larger.  The cache below fills only
    # the missing prefix/tail, so increasing a requirement never discards and
    # rebuilds the already persisted window.
    warmup_bars = _required_warmup_bars(spec)
    tf_ms = _timeframe_ms(spec.timeframe)
    until = int(time.time() * 1000)
    # Anchor the requested prefix to a canonical close boundary.  Subtracting
    # from an arbitrary wall-clock minute can otherwise shave one closed bar
    # off both ends and leave ``requirement + 1`` still non-evaluable.
    last_closed_boundary = (until // tf_ms) * tf_ms
    since = last_closed_boundary - warmup_bars * tf_ms
    cache_path = journal_dir / f"{spec.lane_id}.candles.parquet"
    canonical_store = CandleParquetStore(
        Path(os.environ.get("VNEDGE_CANDLE_ROOT", "data/candles")),
        exchange=spec.exchange,
    )
    canonical_subscription: CanonicalCandleSubscription | None = None
    if canonical_router is not None and spec.exchange in canonical_router_exchanges:
        # Subscribe before any durable warm-up read. Events published while a
        # lane builds remain queued; duplicates through the captured watermark
        # are discarded only after Parquet proves the handoff boundary.
        canonical_subscription = canonical_router.subscribe(
            spec.exchange,
            spec.data_symbol,
            spec.timeframe,
        )
        await warm_subscription_from_store(
            canonical_subscription,
            canonical_store,
            not_before=datetime.fromtimestamp(since / 1000, tz=UTC),
        )
    funding_history_unsupported = False
    allow_validated_exchange_ohlcv = _allows_validated_exchange_ohlcv(spec)
    context_seed_frames: dict[str, pd.DataFrame] = {}
    async with CcxtPublicClient(spec.exchange) as rest:
        # (B) Cache + gap-fill: reuse the persisted candle window and fetch only
        # the gap since the last run; degrades to a full fetch on any cache miss.
        history = await _warmup_candles(rest, spec, cache_path, since, until)
        try:
            canonical_history = await asyncio.to_thread(
                _canonical_candle_frame,
                canonical_store,
                spec.symbol,
                spec.timeframe,
                since_ms=since,
                until_ms=until,
            )
        except (OSError, ValueError):
            logger.exception(
                "lane %s canonical warm-up overlay unavailable; exact fields remain missing",
                spec.lane_id,
            )
            canonical_history = pd.DataFrame()
        history = _overlay_canonical_history(
            history,
            canonical_history,
            allow_validated_exchange_ohlcv=allow_validated_exchange_ohlcv,
        )
        strategy_requirement = _strategy_warmup_requirement(spec)
        if len(history) <= strategy_requirement:
            raise RuntimeError(
                f"lane {spec.lane_id} warmup incomplete: {len(history)} bars; "
                f"strategy requires more than {strategy_requirement}"
            )
        # V2 deliberately declares an official price-only OHLC contract. Its
        # daily EMA200 and weekly structure need an independent HTF window;
        # reusing the 15m ``since`` timestamp made readiness impossible.
        runtime_contract = scanner_runtime_contract(spec.strategy_id)
        if allow_validated_exchange_ohlcv and runtime_contract is not None:
            for context_timeframe in runtime_contract.context_tfs:
                context_since = _warmup_since_for_timeframe(
                    context_timeframe,
                    until,
                    bars=_CONTEXT_WARMUP_BARS,
                )
                context_spec = replace(spec, timeframe=context_timeframe)
                exchange_context = await _warmup_candles(
                    rest,
                    context_spec,
                    journal_dir
                    / f"{spec.lane_id}.context_{context_timeframe}.candles.parquet",
                    context_since,
                    until,
                )
                try:
                    exact_context = await asyncio.to_thread(
                        _canonical_candle_frame,
                        canonical_store,
                        spec.symbol,
                        context_timeframe,
                        since_ms=context_since,
                        until_ms=until,
                    )
                except (OSError, ValueError):
                    logger.exception(
                        "lane %s canonical %s context overlay unavailable; "
                        "using validated official OHLC for the registered V2 contract",
                        spec.lane_id,
                        context_timeframe,
                    )
                    exact_context = pd.DataFrame()
                context_seed_frames[context_timeframe] = _overlay_canonical_history(
                    exchange_context,
                    exact_context,
                    allow_validated_exchange_ohlcv=True,
                )
        try:
            raw_f = await rest.fetch_funding_history(spec.symbol, since, until)
        except NotSupported:
            funding_history_unsupported = True
            if _requires_funding_history(spec.strategy_id):
                # No CCXT funding history (Delta). Don't fail the lane — try
                # the native FUNDING: candle backfill below; failing that,
                # build the window live from the funding stream and persist it.
                logger.info(
                    "%s %s: no CCXT funding history; %s will seed from the "
                    "native backfill or accumulate funding live",
                    spec.exchange,
                    spec.symbol,
                    spec.strategy_id,
                )
            else:
                logger.info(
                    "%s %s: funding history unavailable; running %s with zero funding",
                    spec.exchange,
                    spec.symbol,
                    spec.strategy_id,
                )
            raw_f = []
    seed_funding = normalize_funding(raw_f)
    if funding_history_unsupported and _requires_funding_history(spec.strategy_id):
        seed_funding = await _delta_funding_seed(spec, seed_funding)
    # The persistent accumulator (live samples + fsync'd store + warmup mask)
    # stays in charge whenever the venue can't REST-seed funding via CCXT —
    # the native backfill only SEEDS it. Venues with CCXT funding history
    # keep the proven REST-seeded LiveFundingMR path (store path None).
    funding_store_path = (
        journal_dir / f"{spec.lane_id}.funding.jsonl"
        if funding_history_unsupported or seed_funding.empty
        else None
    )

    # Shared-feed registry: lanes on the same (exchange, symbol, timeframe) —
    # e.g. the governed paper lane and its shadow twin — share ONE real feed;
    # each lane gets a view with its own closed-candle queue (fan-out, not
    # competition), and the last lane to stop tears the real feed down.
    feed = acquire_market_feed(spec.exchange, symbol=spec.symbol, timeframe=spec.timeframe)
    if not history.empty:
        # REST warm-up already owns every bar through this timestamp.  Prime
        # the live feed's monotonic emission guard before it starts so the
        # latest cached bar is not replayed as a new close (and recorded as a
        # multi-minute/hour transport-latency breach).
        latest_open_ms = int(
            pd.to_datetime(history["timestamp"].iloc[-1], utc=True).timestamp() * 1000
        )
        feed.prime_closed_through(latest_open_ms)
    risk = _lane_risk_config(spec)
    daily_factory = _lane_daily_factory_config(spec)
    runtime_contract = scanner_runtime_contract(spec.strategy_id)
    if runtime_contract is not None and runtime_contract.timeframe != spec.timeframe:
        raise ValueError(
            f"{spec.strategy_id} runtime contract requires "
            f"{runtime_contract.timeframe}, got {spec.timeframe}"
        )
    config = RunnerConfig(
        mode=spec.mode,
        symbol=spec.symbol,
        timeframe=spec.timeframe,
        starting_equity_usd=spec.starting_equity,
        risk=risk,
        limits=venue_symbol_limits(spec.exchange, spec.symbol),
        daily_factory=daily_factory,
        max_holding_bars=(
            runtime_contract.max_holding_bars if runtime_contract is not None else 48
        ),
        canonical_candle_wait_seconds=8.0,
        trail_atr_mult=spec.trail_atr_mult,
        execution_cost_exchange_id=spec.execution_cost_exchange,
    )
    strategy = _build_strategy(spec, seed_funding, feed, funding_store_path=funding_store_path)
    quote_evidence = (
        QuoteEvidenceRecorder(
            Path(os.environ.get("VNEDGE_QUOTE_EVIDENCE_ROOT", "data/quote_evidence")),
            lane_id=spec.lane_id,
            exchange=spec.exchange,
            symbol=spec.symbol,
            max_queue=int(os.environ.get("VNEDGE_QUOTE_EVIDENCE_QUEUE", "8192")),
        )
        if (
            _env_bool(os.environ, "VNEDGE_QUOTE_EVIDENCE_ENABLED")
            and runtime_contract is not None
            and runtime_contract.decision_engine.startswith("quote_acceptance")
        )
        else None
    )
    declared_context_timeframes = tuple(
        str(value) for value in getattr(strategy, "canonical_context_timeframes", ())
    )
    if (
        runtime_contract is not None
        and declared_context_timeframes != runtime_contract.context_tfs
    ):
        raise ValueError(
            f"{spec.strategy_id} runtime context contract requires "
            f"{runtime_contract.context_tfs}, strategy declares "
            f"{declared_context_timeframes}"
        )
    context_timeframes = (
        runtime_contract.context_tfs
        if runtime_contract is not None
        else declared_context_timeframes
    )
    context_watermarks: dict[str, datetime] = {}
    context_binder = getattr(strategy, "bind_canonical_context", None)
    if context_timeframes and callable(context_binder):
        for context_timeframe in context_timeframes:
            context_history = context_seed_frames.get(context_timeframe)
            if context_history is None:
                context_since = _warmup_since_for_timeframe(
                    context_timeframe,
                    until,
                    bars=_CONTEXT_WARMUP_BARS,
                )
                try:
                    context_history = await asyncio.to_thread(
                        _canonical_candle_frame,
                        canonical_store,
                        spec.symbol,
                        context_timeframe,
                        since_ms=context_since,
                        until_ms=until,
                    )
                except (OSError, ValueError):
                    logger.exception(
                        "lane %s canonical %s context unavailable; scanner remains fail-closed",
                        spec.lane_id,
                        context_timeframe,
                    )
                    context_history = pd.DataFrame()
            context_binder(context_timeframe, context_history)
            if not context_history.empty:
                opened = pd.Timestamp(context_history["timestamp"].iloc[-1])
                if opened.tzinfo is None:
                    opened = opened.tz_localize("UTC")
                else:
                    opened = opened.tz_convert("UTC")
                context_watermarks[context_timeframe] = (
                    opened + pd.Timedelta(context_timeframe)
                ).to_pydatetime()
    exchange = SimulatedExchange(venue_fill_model(spec.exchange), config.starting_equity_usd)
    journal = DecisionJournal(journal_dir / f"{spec.lane_id}.journal.jsonl")
    kill = KillSwitch(kill_file=journal_dir / f"{spec.lane_id}.KILL")
    gateway = PreTradeRiskGateway(config.risk, kill)
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    session = LivePaperSession(
        strategy,
        feed,
        history,
        config,
        gateway=gateway,
        order_manager=om,
        exchange=exchange,
        journal=journal,
        snapshot_provider=provider.sink(spec.lane_id, spec.exchange),
        account_store=PaperAccountStore(journal_dir / f"{spec.lane_id}.account.json", spec.lane_id),
        equity_history_path=journal_dir / f"{spec.lane_id}.equity.jsonl",
        fill_ledger=FillLedger(journal_dir / f"{spec.lane_id}.fills.jsonl"),
        funnel_store=LaneFunnelStore(journal_dir / f"{spec.lane_id}.funnel.json", spec.lane_id),
        latency_store=LaneLatencyStore(journal_dir / f"{spec.lane_id}.latency.json", spec.lane_id),
        gap_store=GapParquetStore(Path(os.environ.get("VNEDGE_GAP_ROOT", "data/gaps"))),
        shadow_portfolio=shadow_portfolio,
        canonical_candle_store=_canonical_runtime_store(spec, canonical_store),
        canonical_candle_subscription=canonical_subscription,
        quote_evidence=quote_evidence,
        canonical_arm_health=canonical_arm_health,
        canonical_context_watermarks=context_watermarks,
        trial_meta={
            "trial_id": spec.lane_id,
            "started": "2026-07-04",
            "min_days": 14,
            "preferred_days": 30,
            "min_trades": 10,
            "max_dd_pct": 6.0,
            "daily_stop_usd": spec.daily_loss_usd,
            "promotion_source": spec.exchange,
            "daily_factory": daily_factory.model_dump(),
        },
    )
    # Expectations make a moved/edited store fail closed instead of injecting
    # a wrong-symbol position or absurd balance into the lane.
    resumed = session.account_store.restore_into(
        exchange,
        session.tracker,
        expected_symbol=spec.symbol,
        expected_starting_equity=spec.starting_equity,
    )
    if resumed:
        state = session.account_store.load() or {}
        session.restore_plan(state.get("plan"))
    # Resume the funnel counters so the live activity view doesn't reset to 0
    # on every deploy (display-only; never gates a trade).
    session.funnel_store.restore_into(session)
    session.latency_store.restore_into(session.latency)
    logger.info(
        "lane %s (%s %s %s %s) built; resumed=%s",
        spec.lane_id,
        spec.exchange,
        spec.symbol,
        spec.strategy_id,
        spec.mode.value,
        resumed,
    )
    if quote_evidence is not None:
        quote_evidence.start()
    return _LaneRuntime(spec=spec, session=session, feed=feed)


class MultiLaneShadowRunner:
    def __init__(
        self,
        specs: list[LaneSpec],
        journal_dir: Path,
        provider: MultiLaneProvider,
        *,
        canonical_router: CanonicalCandleRouter | None = None,
        canonical_producers: tuple[CanonicalProducer, ...] = (),
        canonical_router_exchanges: frozenset[str] = frozenset(),
    ) -> None:
        self.specs = specs
        self.journal_dir = journal_dir
        self.provider = provider
        self.canonical_router = canonical_router
        self.canonical_producers = canonical_producers
        self.canonical_router_exchanges = canonical_router_exchanges
        observers = [
            spec
            for spec in specs
            if spec.mode is RunnerMode.SHADOW and spec.strategy_id != "measurement_only_v1"
        ]
        shared_equity = min(
            (Decimal(str(spec.starting_equity)) for spec in observers),
            default=Decimal(1000),
        )
        shared_daily_loss = min(
            (Decimal(str(spec.daily_loss_usd)) for spec in observers),
            default=Decimal(20),
        )
        self.shadow_portfolio = ShadowPortfolioGate(
            journal_dir=journal_dir,
            lane_ids=(spec.lane_id for spec in observers),
            equity_usd=shared_equity,
            daily_loss_limit_usd=shared_daily_loss,
        )

    def _canonical_arm_health(self, spec: LaneSpec) -> Callable[[], str | None] | None:
        """Bind an integrated producer's durability probe to one lane.

        External-Parquet lanes retain their existing lake/prerequisite gates.
        In integrated-dark mode, missing or unreadable producer health fails
        closed for new arms while leaving reduce-only exits untouched.
        """
        if spec.exchange not in self.canonical_router_exchanges:
            return None
        producers = tuple(
            producer
            for producer in self.canonical_producers
            if str(getattr(producer, "exchange_id", "")) == spec.exchange
        )

        def probe() -> str | None:
            if not producers:
                return "canonical_producer_missing"
            for producer in producers:
                check = getattr(producer, "new_arm_block_reason", None)
                if not callable(check):
                    return "canonical_producer_health_unavailable"
                reason = check(spec.symbol)
                if reason is None:
                    return None
                if reason != "canonical_producer_symbol_unowned":
                    return reason
            return "canonical_producer_symbol_unowned"

        return probe

    async def run(self, *, deadline_seconds: float | None = None) -> None:
        # (C) Show the whole fleet immediately as "warming up" while it builds,
        # so the dashboard is never a blank board mid-startup.
        for spec in self.specs:
            self.provider.publish_warming(spec.lane_id, spec.exchange, spec.symbol)
        # Bounded concurrency + transient-retry so the whole fleet comes up: a
        # naked gather over every lane bursts the venue APIs and rate-limits a
        # chunk of them into permanent build failures for the session.
        build_sem = asyncio.Semaphore(_lane_build_concurrency())

        async def _build(spec: LaneSpec) -> _LaneRuntime:
            async with build_sem:
                return await _retry_transient(
                    lambda: build_lane(
                        spec,
                        self.provider,
                        self.journal_dir,
                        self.shadow_portfolio,
                        self.canonical_router,
                        self.canonical_router_exchanges,
                        self._canonical_arm_health(spec),
                    ),
                    retries=_LANE_BUILD_RETRIES,
                    backoff_s=_LANE_BUILD_BACKOFF_S,
                    label=spec.lane_id,
                )

        results = await asyncio.gather(
            *[_build(s) for s in self.specs],
            return_exceptions=True,
        )
        runtimes: list[_LaneRuntime] = []
        for spec, result in zip(self.specs, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "lane %s (%s %s) failed to build: %s",
                    spec.lane_id,
                    spec.exchange,
                    spec.symbol,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )
                self.provider.publish_error(
                    spec.lane_id,
                    spec.exchange,
                    spec.symbol,
                    f"build failed: {result}",
                )
                continue
            runtimes.append(result)

        started: list[_LaneRuntime] = []
        for runtime in runtimes:
            try:
                await runtime.feed.start()
            except Exception as exc:
                logger.error(
                    "lane %s (%s %s) feed failed to start: %s",
                    runtime.spec.lane_id,
                    runtime.spec.exchange,
                    runtime.spec.symbol,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                self.provider.publish_error(
                    runtime.spec.lane_id,
                    runtime.spec.exchange,
                    runtime.spec.symbol,
                    f"feed start failed: {exc}",
                )
                continue
            started.append(runtime)

        if not started:
            raise RuntimeError("no multi-lane shadow lanes started")

        logger.info(
            "multi-lane shadow: %d/%d lanes running (%s)",
            len(started),
            len(self.specs),
            ", ".join(r.spec.lane_id for r in started),
        )
        producer_tasks = [
            asyncio.create_task(producer.run(), name=f"canonical-producer-{index}")
            for index, producer in enumerate(self.canonical_producers)
        ]
        lane_tasks = [
            asyncio.create_task(
                self._run_lane(runtime, deadline_seconds=deadline_seconds),
                name=f"lane-{runtime.spec.lane_id}",
            )
            for runtime in started
        ]
        try:
            await asyncio.gather(*producer_tasks, *lane_tasks)
        finally:
            for task in (*producer_tasks, *lane_tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*producer_tasks, *lane_tasks, return_exceptions=True)

    async def _run_lane(self, runtime: _LaneRuntime, *, deadline_seconds: float | None) -> None:
        try:
            await runtime.session.run(deadline_seconds=deadline_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "lane %s (%s %s) stopped with error",
                runtime.spec.lane_id,
                runtime.spec.exchange,
                runtime.spec.symbol,
            )
            self.provider.publish_error(
                runtime.spec.lane_id,
                runtime.spec.exchange,
                runtime.spec.symbol,
                f"session failed: {exc}",
            )
        finally:
            quote_evidence = runtime.session.quote_evidence
            if quote_evidence is not None:
                try:
                    await quote_evidence.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "lane %s quote evidence close failed: %s",
                        runtime.spec.lane_id,
                        exc,
                    )
            try:
                await runtime.feed.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("lane %s feed stop failed: %s", runtime.spec.lane_id, exc)
