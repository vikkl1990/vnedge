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
import time
from dataclasses import dataclass
from pathlib import Path

from ccxt.base.errors import NotSupported

from vnedge.config.risk_config import RiskConfig
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.schemas import normalize_candles, normalize_funding
from vnedge.exchange.live_feed import (
    LiveMarketFeed,
    RestPollingMarketFeed,
    create_market_feed,
)
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.signal_arbiter import ArbiterConfig, SignalArbiter
from vnedge.paper.account_store import PaperAccountStore
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.live_paper import LivePaperSession
from vnedge.runtime.paper_trial import LiveFundingMR
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.composite import CompositeSignalStrategy
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.scalper_1m import Scalper1m
from vnedge.strategy.trend_continuation import TrendContinuation
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LaneSpec:
    lane_id: str          # unique, e.g. "binance_funding_mr"
    exchange: str         # ccxt id, e.g. "binanceusdm" | "bybit"
    symbol: str
    timeframe: str = "1h"
    starting_equity: float = 500.0
    strategy_params: dict | None = None
    is_primary: bool = False   # the governed lane shown as the flat snapshot
    daily_loss_usd: float = 10.0
    mode: RunnerMode = RunnerMode.SHADOW
    strategy_id: str = "funding_mean_reversion_v1"


@dataclass(frozen=True)
class _LaneRuntime:
    spec: LaneSpec
    session: LivePaperSession
    feed: LiveMarketFeed | RestPollingMarketFeed


# --- snapshot fan-in --------------------------------------------------------------

class _LaneSink:
    """A provider-shaped object one lane publishes to; tags + forwards."""

    def __init__(self, parent: "MultiLaneProvider", lane_id: str, exchange: str) -> None:
        self._parent, self._lane_id, self._exchange = parent, lane_id, exchange

    def publish(self, snapshot: dict) -> None:
        self._parent._publish(self._lane_id, self._exchange, snapshot)


class MultiLaneProvider:
    """Holds each lane's latest snapshot. latest() returns the primary lane
    (flat, backward-compatible) with a `lanes` array appended for comparison."""

    def __init__(self, primary_lane_id: str) -> None:
        self.primary = primary_lane_id
        self._lanes: dict[str, dict] = {}
        self._order: list[str] = []

    def sink(self, lane_id: str, exchange: str) -> _LaneSink:
        return _LaneSink(self, lane_id, exchange)

    def _publish(self, lane_id: str, exchange: str, snapshot: dict) -> None:
        snap = dict(snapshot)
        snap["lane_id"] = lane_id
        snap["lane_exchange"] = exchange
        if lane_id not in self._lanes:
            self._order.append(lane_id)
        self._lanes[lane_id] = snap

    def publish_error(
        self, lane_id: str, exchange: str, symbol: str, error: str
    ) -> None:
        self._publish(lane_id, exchange, {
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
            "recent_alerts": [{
                "severity": "critical",
                "message": error,
                "rule_id": "lane_error",
            }],
            "session": {"lane_error": error},
        })

    def latest(self) -> dict | None:
        if not self._lanes:
            return None
        primary = self._lanes.get(self.primary) or self._lanes[self._order[0]]
        out = dict(primary)
        out["lanes"] = [
            {
                "lane_id": self._lanes[lid]["lane_id"],
                "exchange": self._lanes[lid]["lane_exchange"],
                "symbol": self._lanes[lid].get("symbol", ""),
                # per-lane mode + strategy so the dashboard lane matrix can
                # label paper vs shadow and which strategy each lane runs
                "mode": self._lanes[lid].get("mode", ""),
                "strategy_id": self._lanes[lid].get("strategy_id", ""),
                "equity": self._lanes[lid].get("equity", 0.0),
                "realized_pnl": self._lanes[lid].get("realized_pnl", 0.0),
                "unrealized_pnl": self._lanes[lid].get("unrealized_pnl", 0.0),
                "fills": self._lanes[lid].get("fills", 0),
                "fees_usd": self._lanes[lid].get("fees_usd", 0.0),
                "risk_status": self._lanes[lid].get("risk_status", "?"),
                "feed": self._lanes[lid].get("feed_health", {}).get("candles", "?"),
                "positions": len(self._lanes[lid].get("positions", [])),
            }
            for lid in self._order if lid in self._lanes
        ]
        return out


# --- lane construction ------------------------------------------------------------

_FUNDING_HISTORY_REQUIRED = {"funding_mean_reversion_v1"}


def _requires_funding_history(strategy_id: str) -> bool:
    return strategy_id in _FUNDING_HISTORY_REQUIRED


def _build_strategy(
    spec: LaneSpec, seed_funding, feed
) -> BaseStrategy:
    """Construct the live strategy a lane runs, keyed by strategy_id."""
    params = spec.strategy_params or {}
    if spec.strategy_id == "signal_arbiter_v1":
        return _build_signal_arbiter_strategy(spec, seed_funding, feed)
    return _build_single_strategy(spec.strategy_id, params, seed_funding, feed)


def _build_single_strategy(
    strategy_id: str,
    params: dict,
    seed_funding,
    feed,
) -> BaseStrategy:
    """Construct one strategy implementation for a live-data lane."""
    if strategy_id == "funding_mean_reversion_v1":
        # needs the funding stream augmented live off the feed
        return LiveFundingMR(seed_funding, feed, **params)
    if strategy_id == "trend_continuation_v1":
        # candle-only; funding is a mild static filter (fine for a shadow lane)
        return TrendContinuation(seed_funding, **params)
    if strategy_id == "volatility_expansion_breakout_v1":
        return VolatilityExpansionBreakout(seed_funding, **params)
    if strategy_id == "panic_reversal_v1":
        return PanicReversal(seed_funding, **params)
    if strategy_id == "funding_squeeze_continuation_v1":
        return FundingSqueezeContinuation(seed_funding, **params)
    if strategy_id == "scalper_1m_v1":
        return Scalper1m(seed_funding, **params)
    raise ValueError(f"unsupported lane strategy_id: {strategy_id!r}")


def _build_signal_arbiter_strategy(spec: LaneSpec, seed_funding, feed) -> BaseStrategy:
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
            raise ValueError("signal_arbiter_v1 child entries must be objects")
        child_strategy_id = str(child.get("strategy_id", ""))
        if not child_strategy_id:
            raise ValueError("signal_arbiter_v1 child missing strategy_id")
        child_params = child.get("params", {})
        if not isinstance(child_params, dict):
            raise ValueError("signal_arbiter_v1 child params must be an object")

        strategies.append(
            _build_single_strategy(child_strategy_id, child_params, seed_funding, feed)
        )
        default_source_id = f"{child_strategy_id}#{index + 1}"
        source_id = str(child.get("source_id", default_source_id))
        candidate_defaults[default_source_id] = {"source_id": source_id}
        candidate_defaults[source_id] = {
            key: child[key]
            for key in edge_keys
            if key in child
        }

    arbiter_params = params.get("arbiter", {})
    if not isinstance(arbiter_params, dict):
        raise ValueError("signal_arbiter_v1 arbiter config must be an object")
    return CompositeSignalStrategy(
        strategies,
        SignalArbiter(ArbiterConfig(**arbiter_params)),
        symbol=spec.symbol,
        candidate_defaults=candidate_defaults,
        strategy_id=spec.strategy_id,
    )


async def build_lane(
    spec: LaneSpec, provider: MultiLaneProvider, journal_dir: Path
) -> _LaneRuntime:
    """Seed warmup history + build an isolated LivePaperSession for one venue."""
    warmup_hours = 450
    until = int(time.time() * 1000)
    since = until - warmup_hours * 3_600_000
    async with CcxtPublicClient(spec.exchange) as rest:
        raw_c = await rest.fetch_candles(spec.symbol, spec.timeframe, since, until)
        try:
            raw_f = await rest.fetch_funding_history(spec.symbol, since, until)
        except NotSupported as exc:
            if _requires_funding_history(spec.strategy_id):
                raise ValueError(
                    f"{spec.exchange} does not expose funding history needed by "
                    f"{spec.strategy_id}"
                ) from exc
            logger.info(
                "%s %s: funding history unavailable; running %s with zero funding",
                spec.exchange, spec.symbol, spec.strategy_id,
            )
            raw_f = []
    history = normalize_candles(raw_c)
    seed_funding = normalize_funding(raw_f)

    feed = create_market_feed(spec.exchange, symbol=spec.symbol, timeframe=spec.timeframe)
    risk = RiskConfig(max_daily_loss_usd=spec.daily_loss_usd, max_daily_loss_pct=2.0)
    config = RunnerConfig(mode=spec.mode, symbol=spec.symbol,
                          timeframe=spec.timeframe,
                          starting_equity_usd=spec.starting_equity, risk=risk)
    strategy = _build_strategy(spec, seed_funding, feed)
    exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
    journal = DecisionJournal(journal_dir / f"{spec.lane_id}.journal.jsonl")
    kill = KillSwitch(kill_file=journal_dir / f"{spec.lane_id}.KILL")
    gateway = PreTradeRiskGateway(config.risk, kill)
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    session = LivePaperSession(
        strategy, feed, history, config,
        gateway=gateway, order_manager=om, exchange=exchange, journal=journal,
        snapshot_provider=provider.sink(spec.lane_id, spec.exchange),
        account_store=PaperAccountStore(
            journal_dir / f"{spec.lane_id}.account.json", spec.lane_id),
        equity_history_path=journal_dir / f"{spec.lane_id}.equity.jsonl",
        trial_meta={"trial_id": spec.lane_id, "started": "2026-07-04",
                    "min_days": 14, "preferred_days": 30, "min_trades": 10,
                    "max_dd_pct": 6.0, "daily_stop_usd": spec.daily_loss_usd,
                    "promotion_source": spec.exchange},
    )
    resumed = session.account_store.restore_into(exchange, session.tracker)
    logger.info("lane %s (%s %s %s %s) built; resumed=%s",
                spec.lane_id, spec.exchange, spec.symbol, spec.strategy_id,
                spec.mode.value, resumed)
    return _LaneRuntime(spec=spec, session=session, feed=feed)


class MultiLaneShadowRunner:
    def __init__(self, specs: list[LaneSpec], journal_dir: Path,
                 provider: MultiLaneProvider) -> None:
        self.specs = specs
        self.journal_dir = journal_dir
        self.provider = provider

    async def run(self, *, deadline_seconds: float | None = None) -> None:
        results = await asyncio.gather(
            *[build_lane(s, self.provider, self.journal_dir) for s in self.specs],
            return_exceptions=True,
        )
        runtimes: list[_LaneRuntime] = []
        for spec, result in zip(self.specs, results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "lane %s (%s %s) failed to build: %s",
                    spec.lane_id, spec.exchange, spec.symbol, result,
                    exc_info=(type(result), result, result.__traceback__),
                )
                self.provider.publish_error(
                    spec.lane_id, spec.exchange, spec.symbol,
                    f"build failed: {result}",
                )
                continue
            runtimes.append(result)

        started: list[_LaneRuntime] = []
        for runtime in runtimes:
            try:
                await runtime.feed.start()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "lane %s (%s %s) feed failed to start: %s",
                    runtime.spec.lane_id, runtime.spec.exchange,
                    runtime.spec.symbol, exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                self.provider.publish_error(
                    runtime.spec.lane_id, runtime.spec.exchange,
                    runtime.spec.symbol, f"feed start failed: {exc}",
                )
                continue
            started.append(runtime)

        if not started:
            raise RuntimeError("no multi-lane shadow lanes started")

        logger.info("multi-lane shadow: %d/%d lanes running (%s)",
                    len(started), len(self.specs),
                    ", ".join(r.spec.lane_id for r in started))
        await asyncio.gather(*[
            self._run_lane(runtime, deadline_seconds=deadline_seconds)
            for runtime in started
        ])

    async def _run_lane(
        self, runtime: _LaneRuntime, *, deadline_seconds: float | None
    ) -> None:
        try:
            await runtime.session.run(deadline_seconds=deadline_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "lane %s (%s %s) stopped with error: %s",
                runtime.spec.lane_id, runtime.spec.exchange,
                runtime.spec.symbol, exc,
            )
            self.provider.publish_error(
                runtime.spec.lane_id, runtime.spec.exchange,
                runtime.spec.symbol, f"session failed: {exc}",
            )
        finally:
            try:
                await runtime.feed.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "lane %s feed stop failed: %s", runtime.spec.lane_id, exc
                )
