"""Live paper session — real-time market data, simulated execution.

The mode ladder's "paper" rung with LIVE data instead of replay: closed
candles arrive from the websocket feed, strategy decisions happen at bar
close, and orders execute immediately at the live quote (which IS the next
bar's start) — the same discipline the backtester and replay runner enforce.

The order path is IDENTICAL to replay and (later) live: strategy → risk
gateway → journal → OrderManager → broker. This module owns only the loop
around it. Staleness is real here: the gateway evaluates against wall-clock
`now` vs the feed's last websocket event, so a stalled stream blocks entries
naturally.

Incremental data-quality gate at the boundary: candles must arrive strictly
forward in time; anything else is dropped and counted, never processed.

Stops additionally get TICK granularity: between bar closes the idle loop
checks the live top-of-book against the open plan's stop and exits
reduce-only on breach, through the exact same gateway/journal/OrderManager
pipeline as bar-close exits. Take-profits deliberately remain bar-close —
a stop is capital protection (delay is unbounded downside), a TP is
strategy semantics that research models at bar granularity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from vnedge.dashboard.state_snapshot import FeedHealth, build_snapshot
from vnedge.data.candles import CandleParquetStore
from vnedge.data.gaps import GapKind, GapParquetStore, GapRecord
from vnedge.data.time_machine import TimeMachine
from vnedge.execution.idempotency import make_intent_key
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState
from vnedge.ml.regime_v0 import RegimeV0
from vnedge.paper.paper_reconciliation import PaperReconciler
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.plan.adapters import signal_intent_to_plan
from vnedge.plan.cost_model import CostModel
from vnedge.plan.trade_plan import plan_gate
from vnedge.risk.cost_gate import CostGate, CostProfile
from vnedge.risk.position_sizer import size_position
from vnedge.risk.protections import ProtectionState
from vnedge.risk.risk_manager import MarketState, OrderIntent, PreTradeRiskGateway
from vnedge.runtime import latency_thresholds as LT
from vnedge.runtime.active_exit import (
    ActiveExitDecision,
    ActiveExitState,
    ExitEngine,
    ExitEngineConfig,
)
from vnedge.runtime.daily_factory import (
    entry_block_reason,
    session_day,
    should_force_flatten,
)
from vnedge.runtime.latency_tracker import (
    BAR_CLOSE_PROCESSING_MS,
    DECISION_LAG_MS,
    LatencyTracker,
    timeframe_to_seconds,
)
from vnedge.runtime.portfolio_tracker import PortfolioTracker
from vnedge.runtime.run_report import RunReport
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.runtime.scanner_session import SessionCosts
from vnedge.runtime.shadow_outcomes import (
    ShadowOutcomeTracker,
    VirtualOutcome,
    is_maker_route_strategy,
)
from vnedge.runtime.shadow_portfolio import ShadowPortfolioGate
from vnedge.runtime.squeeze_acceptance_observe import (
    SqueezeAcceptanceObserveRunner,
)
from vnedge.runtime.squeeze_observe import ScannerApproval, SqueezeObserveRunner
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent, StrategyExitIntent
from vnedge.strategy.indicators import atr as _atr_indicator

logger = logging.getLogger(__name__)


def _append_equity_history(path: str | Path, now: datetime, equity: float) -> None:
    """Append one equity sample without blocking the asyncio decision loop."""
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"ts": now.isoformat(), "equity": round(equity, 4)}) + "\n"
        )

_EXIT_ACCEPTED_STATES = frozenset(
    {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}
)
_EXIT_RETRYABLE_STATES = frozenset(
    {OrderState.RISK_REJECTED, OrderState.REJECTED, OrderState.CANCELLED}
)


def _extract_strategy_thresholds(
    strategy: BaseStrategy, threshold_names: tuple[str, ...]
) -> dict[str, float]:
    """Read scanner thresholds from either the strategy or its frozen params."""
    params = getattr(strategy, "params", None)
    thresholds: dict[str, float] = {}
    for attr in threshold_names:
        val = getattr(strategy, attr, None)
        if not _is_numeric(val) and params is not None:
            val = getattr(params, attr, None)
        if _is_numeric(val):
            thresholds[attr] = float(val)
    return thresholds


def _is_numeric(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _signal_payload(sig: SignalIntent | None) -> dict | None:
    if sig is None:
        return None
    return {
        "side": sig.side,
        "stop_price": sig.stop_price,
        "take_profit_price": sig.take_profit_price,
        "take_profit_levels": list(sig.take_profit_levels),
        "reason": sig.reason,
    }


@dataclass
class _LivePlan:
    signal: SignalIntent
    entry_bar_ts: pd.Timestamp
    exit_state: ActiveExitState | None = None

    def __post_init__(self) -> None:
        if self.exit_state is None:
            self.exit_state = ActiveExitState.from_signal(self.signal)


@dataclass
class _PendingMakerEntry:
    """A maker-routed entry resting as a limit, not yet filled.

    Maker-edge strategies post a passive limit instead of crossing the spread;
    it becomes a position only when a later quote touches it (a real maker fill),
    and is cancelled — the trade skipped — if no touch lands within its TTL. The
    immediate runners a maker would miss are missed here too (adverse selection),
    mirroring the shadow-lane discipline.
    """

    plan: _LivePlan
    client_order_id: str
    bars_waited: int = 0


class LivePaperSession:
    def __init__(
        self,
        strategy: BaseStrategy,
        feed,  # LiveMarketFeed or a fake with the same surface
        history: pd.DataFrame,  # warmup seed, gate-validated canonical candles
        config: RunnerConfig,
        *,
        gateway: PreTradeRiskGateway,
        order_manager: OrderManager,
        exchange: SimulatedExchange,
        journal: DecisionJournal,
        snapshot_provider=None,  # optional dashboard hookup
        account_store=None,  # optional PaperAccountStore for crash/restart resume
        alert_engine=None,  # optional AlertEngine — same snapshot, guarded fanout
        equity_history_path=None,  # optional JSONL of (ts, equity) per bar
        trial_meta=None,  # optional dict shown on the dashboard governance panel
        fill_ledger=None,  # optional FillLedger — hash-chained execution record
        funnel_store=None,  # optional LaneFunnelStore — resume counters on restart
        latency_store=None,  # optional LaneLatencyStore — resume p95 samples
        gap_store: GapParquetStore | None = None,
        shadow_portfolio: ShadowPortfolioGate | None = None,
        canonical_candle_store: CandleParquetStore | None = None,
    ) -> None:
        self.strategy = strategy
        self.feed = feed
        self.candles = history.reset_index(drop=True)
        self.config = config
        self.gateway = gateway
        self.om = order_manager
        self.exchange = exchange
        self.journal = journal
        self._backfill_eval_keys: set[tuple[str, str, str, str]] = {
            (
                str(payload.get("strategy_id", "")),
                str(payload.get("symbol", "")),
                str(payload.get("timeframe", "")),
                str(payload.get("bar_ts", "")),
            )
            for record in journal.read_all()
            for payload in [record.get("payload")]
            if record.get("kind") == "lane_eval"
            and isinstance(payload, dict)
            and bool(payload.get("backfill"))
        }
        self.provider = snapshot_provider
        self.account_store = account_store
        self.alert_engine = alert_engine
        self.equity_history_path = equity_history_path
        self.fill_ledger = fill_ledger
        self.funnel_store = funnel_store
        self.latency_store = latency_store
        self.gap_store = gap_store
        self.shadow_portfolio = shadow_portfolio
        self.canonical_candle_store = canonical_candle_store
        # when this lane last fired a LIVE signal — lets the dashboard show
        # "last fired 2d ago" so a slow, quiet lane reads as waiting, not dead
        self.last_fired_ts: str | None = None
        # baseline against the EXCHANGE's fill list (resets each session), not
        # the ledger's total record count (survives restarts) — else every
        # post-restart fill would be sliced away and never chained/logged
        self._ledgered_fills = len(exchange.get_fills())
        self.trial_meta = trial_meta
        self.bars_processed = 0
        self._started_at = datetime.now(UTC)
        # pipeline latency: bar_close_processing (candle close -> we act) +
        # decision_lag (candle in hand -> signal). Bad timeframe disables close
        # lag rather than crashing: a broken bar length must not take a lane down.
        self.latency = LatencyTracker()
        try:
            self._tf_seconds: int | None = timeframe_to_seconds(config.timeframe)
        except ValueError:
            logger.warning("unparseable timeframe %r — feed-lag disabled", config.timeframe)
            self._tf_seconds = None
        self._tf_ms: int | None = self._tf_seconds * 1000 if self._tf_seconds else None
        # feed-continuity guard: a WS reconnect can silently skip closed bars,
        # and a wedged loop can leave a bar undelivered. Either would poison the
        # contiguous-index indicators. On a gap we heal (REST gap-fill) or fail
        # closed (reduce-only: block new entries, keep managing exits).
        self._degraded_reason: str | None = None      # reduce-only while set
        self._degraded_recoverable: bool = False       # stall clears on resume; a hole does not
        self._recovery_bars_remaining = 0              # post-backfill clean-bar proof
        self._last_bar_wall: datetime | None = None    # wall clock of last processed bar
        self.gapped_candles = 0                        # time-gaps detected
        self.gap_fills = 0                             # gaps healed by REST backfill
        self.discontinuity_events = 0                  # large open≠prev-close jumps (soft)
        self.future_candles = 0                        # bar claims to close in the future (skew/convention)
        # Time Machine (read-only observability) — multi-TF forming+closed
        # awareness for this lane's timeframe. FAIL-CLOSED: any error updating it
        # is swallowed and never touches the decision/execution path.
        self.time_machine: TimeMachine | None = (
            TimeMachine([config.symbol], [config.timeframe])
            if config.timeframe in {"1m", "5m", "15m", "1h", "4h"} else None
        )
        self._tm_degraded = False
        # candle-path arm-gate skip counter, keyed by coarse reason
        # (decision_tf_stale / _gapped / _future / tm_error / tm_age_hard).
        self._decision_skips: dict[str, int] = {}
        # D-lite runtime wiring (OBSERVE-ONLY): the lane's cost world + what
        # regime_v0 and the cost-aware plan contract WOULD say each decision bar.
        # None of this changes the live decision; it only records for the cockpit.
        tf = config.timeframe
        ex = getattr(feed, "exchange_id", "") or ""
        if tf in {"1m", "5m", "15m"}:
            self.cost_profile = "delta_scalp" if "delta" in ex.lower() else "scalp"
        else:
            self.cost_profile = "swing"
        self.cost_model = CostModel.for_profile(self.cost_profile)
        self.entry_cost_gate = CostGate(CostProfile(self.cost_profile))
        self._regime_model = RegimeV0()
        self._overlay_regime: dict | None = None
        self._overlay_plan: dict | None = None
        self._regime_would_block = 0
        self._plan_gate_rejects = 0
        self._trail_atr_faults = 0     # ATR-compute faults that silently disabled the trail
        self.tracker = PortfolioTracker(exchange, config.starting_equity_usd)
        self.reconciler = PaperReconciler(order_manager, exchange)
        # The first canonical scanner runtime. It deliberately exists only in
        # SHADOW_OBSERVE: research-only strategies never acquire order
        # authority, but their trigger/exit/cost lifecycle now uses the same
        # engines and fee model as replay instead of the legacy fixed-TP
        # SignalIntent path.
        self.scanner_observer: (
            SqueezeObserveRunner | SqueezeAcceptanceObserveRunner | None
        ) = (
            (
                SqueezeAcceptanceObserveRunner(
                    journal=journal,
                    symbol=config.symbol,
                    strategy_id=strategy.strategy_id,
                    approve_fire=self._approve_scanner_fire,
                    costs=SessionCosts.from_profile(
                        self.cost_profile,
                        bar_minutes=(self._tf_seconds or 300) / 60.0,
                    ),
                )
                if strategy.strategy_id in {
                    "squeeze_expansion_breakout_v3",
                    "squeeze_expansion_breakout_v4",
                }
                else SqueezeObserveRunner(
                journal=journal,
                symbol=config.symbol,
                approve_fire=self._approve_scanner_fire,
                costs=SessionCosts.from_profile(
                    self.cost_profile,
                    bar_minutes=(self._tf_seconds or 300) / 60.0,
                ),
                )
            )
            if (
                config.mode is RunnerMode.SHADOW
                and strategy.strategy_id in {
                    "squeeze_expansion_breakout_v2",
                    "squeeze_expansion_breakout_v3",
                    "squeeze_expansion_breakout_v4",
                }
            )
            else None
        )
        self.signals = self.orders_submitted = self.risk_rejects = 0
        self.sizing_skips = self.dropped_candles = self.recon_mismatches = 0
        self.shadow_approved = self.shadow_rejected = 0
        self.evals = self.live_evals = self.backfill_evals = 0
        self.live_signals = self.backfill_signals = 0
        self.tick_stop_exits = 0
        self._last_heartbeat_at: datetime | None = None
        self._shadow_exit_df: pd.DataFrame | None = None
        # SHADOW lanes never fill, so per-lane edge is invisible without
        # virtual resolution: approved intents are resolved forward on
        # closed bars with backtester semantics (journal = durable store).
        self.shadow_outcomes: ShadowOutcomeTracker | None = (
            ShadowOutcomeTracker(
                journal,
                fill_model=exchange.fill_model,
                max_holding_bars=config.max_holding_bars,
                # Strategies whose edge is defined after MAKER fees get the
                # resting-limit route (touch-to-fill + maker entry fee); every
                # other lane stays all-taker. Observability only either way.
                maker_route=is_maker_route_strategy(strategy.strategy_id),
                # Same ATR-chandelier trail as the paper/live ActiveExitState, so
                # a shadow lane predicts its paper twin instead of the legacy
                # fixed-stop exit. Fed the identical _trail_atr() per bar below.
                trail_atr_mult=config.trail_atr_mult,
                cost_model=self.cost_model,
                strategy_exit=self._shadow_strategy_exit,
            )
            if config.mode is RunnerMode.SHADOW and self.scanner_observer is None
            else None
        )
        self.last_eval: dict | None = None
        self.last_reject_reason: str | None = None
        # chronological trade narrative for the dashboard journal panel:
        # fired signals, gateway verdicts, submissions, fills, exits
        from collections import deque
        self.trade_log: deque = deque(maxlen=40)
        self._plan: _LivePlan | None = None
        # A maker-routed entry resting as a limit (touch-to-fill), if any. It is
        # NOT a position until it fills; a new entry cannot fire while it rests.
        self._pending_entry: _PendingMakerEntry | None = None
        self._parked_entries: dict[str, _LivePlan] = {}
        self._pending_exit_orders: dict[str, str] = {}
        self._pending_exit_finals: dict[str, bool] = {}
        self._pending_exit_decisions: dict[str, ActiveExitDecision] = {}
        self._exit_retry_attempts: dict[str, int] = {}
        self._orphan_position_guarded = False
        self._reconciliation_fail_closed = False
        self._bars_since_reconcile = 0
        # Entry protections (post-stop cooldown + stop-window guard) — the
        # same state machine the backtester runs, fed with candle-frame row
        # indexes. Consulted by the entry path ONLY; exits never touch it.
        self.protections = ProtectionState(config.effective_protections())
        self._protection_block_logged = False  # one trade_log event per episode
        self._report_day = None
        self._day_open_equity = config.starting_equity_usd
        self._day_open_fills = 0
        self._factory_day = session_day(self._started_at, self.config.daily_factory)
        self._factory_entries_today = 0
        self._factory_flatten_sent = False

    # --- Internals ---------------------------------------------------------------
    def _sync_quote(self) -> bool:
        if self.feed.quote is None:
            return False
        bid, ask = self.feed.quote
        self.exchange.set_quote(self.config.symbol, bid, ask)
        return True

    def _market_state(self) -> MarketState:
        """Feed state plus this lane's candle-continuity truth.

        The run loop already avoids arming a strategy while degraded. Carrying
        the same state into the central gateway prevents a future/direct caller
        from creating risk through a different path. Reduce-only exits receive
        the same state and remain warning-only at the gateway.
        """
        market = self.feed.market_state()
        if self._degraded_reason is None:
            return market
        quality = "stale" if self._degraded_reason.startswith("feed_stall:") else "gap"
        return replace(
            market,
            data_degraded=True,
            data_quality=quality,
            data_quality_reason=self._degraded_reason,
        )

    def _persist_gap(self, record: GapRecord) -> None:
        if self.gap_store is not None:
            self.gap_store.upsert((record,))

    def _append_candle(self, raw_row: list) -> bool:
        """Incremental quality gate: forward timestamps append; an equal timestamp
        REPLACES the last bar (the warmup seam — the REST seed includes the
        in-progress interval as a partial bar, and the feed later delivers that
        same interval's TRUE close; replacing lets the decision run on the real
        close and keeps a partial bar out of the indicator windows); a strictly
        older timestamp is dropped as non-forward (replay)."""
        ts = pd.to_datetime(raw_row[0], unit="ms", utc=True)
        self._refresh_canonical_tail()
        row: dict[str, object] = {
            "timestamp": ts,
            "open": float(raw_row[1]), "high": float(raw_row[2]),
            "low": float(raw_row[3]), "close": float(raw_row[4]),
            "volume": float(raw_row[5]),
        }
        if self.canonical_candle_store is not None:
            try:
                canonical = self.canonical_candle_store.read_at(
                    self.config.symbol,
                    self.config.timeframe,
                    ts.to_pydatetime(),
                )
            except (OSError, ValueError):
                canonical = None
            if canonical is not None:
                # The lake is authoritative for both prices and volume. A CCXT
                # close is only the low-latency notification that the bucket is
                # ready; it must not create a parallel candle truth.
                row.update(
                    {
                        "open": float(canonical.open),
                        "high": float(canonical.high),
                        "low": float(canonical.low),
                        "close": float(canonical.close),
                        "volume": float(canonical.volume),
                        "quote_volume": float(canonical.quote_volume),
                        "trade_count": canonical.trade_count,
                        "taker_buy_volume": float(canonical.taker_buy_volume),
                        "vwap": (
                            float(canonical.vwap)
                            if canonical.vwap is not None
                            else float("nan")
                        ),
                        "data_quality": "ok",
                        "is_closed": True,
                        "timeframe": self.config.timeframe,
                        "symbol": self.config.symbol,
                        "candle_source": "canonical_tick_lake",
                    }
                )
            else:
                # Never synthesize exact-volume fields from close*volume. Their
                # absence is a decision input: strict scanners fail closed and
                # price-only observers remain explicitly labelled as such.
                row["candle_source"] = "exchange_ohlcv"
        if len(self.candles):
            last_ts = self.candles["timestamp"].iloc[-1]
            if ts == last_ts:
                idx = self.candles.index[-1]
                for col, val in row.items():
                    self.candles.at[idx, col] = val
                return True
            if ts < last_ts:
                self.dropped_candles += 1
                logger.warning("dropped non-forward candle %s", ts)
                return False
        self.candles = pd.concat(
            [self.candles, pd.DataFrame([row])], ignore_index=True
        )
        return True

    def _refresh_canonical_tail(self) -> None:
        """Repair every exchange-only row that now exists in the tick lake.

        The exchange OHLCV close and canonical recorder run concurrently.  A
        close notification can therefore arrive just before its trade-derived
        candle is durable.  V4 consumes only prior exact rows, so reconciling a
        missing rows on the next close removes that race without delaying the
        live notification path.  This is deliberately delta-only: canonical
        rows already reconciled in memory are never revisited.
        """
        if self.canonical_candle_store is None or self.candles.empty:
            return
        source = self.candles.get("candle_source")
        if source is None:
            candidates = self.candles.index
        else:
            candidates = self.candles.index[
                source.fillna("exchange_ohlcv").eq("exchange_ohlcv")
            ]
        if len(candidates) == 0:
            return

        # Real stores expose a bulk read.  One indexed map is dramatically
        # cheaper than opening the same daily Parquet partition once for each
        # of a strategy's 2,000 warm-up rows.  Tiny test/fake stores may keep
        # the single-row contract and fall back below.
        read_all = getattr(self.canonical_candle_store, "read", None)
        canonical_by_open: dict[pd.Timestamp, object] = {}
        if callable(read_all):
            try:
                canonical_by_open = {
                    pd.Timestamp(candle.open_time): candle
                    for candle in read_all(
                        self.config.symbol,
                        self.config.timeframe,
                    )
                }
            except (OSError, ValueError):
                canonical_by_open = {}
        for index in candidates:
            opened = pd.Timestamp(self.candles.at[index, "timestamp"])
            canonical = canonical_by_open.get(opened)
            if canonical is None and not callable(read_all):
                try:
                    canonical = self.canonical_candle_store.read_at(
                        self.config.symbol,
                        self.config.timeframe,
                        opened.to_pydatetime(),
                    )
                except (OSError, ValueError):
                    continue
            if canonical is None:
                continue
            values: dict[str, object] = {
                "open": float(canonical.open),
                "high": float(canonical.high),
                "low": float(canonical.low),
                "close": float(canonical.close),
                "volume": float(canonical.volume),
                "quote_volume": float(canonical.quote_volume),
                "trade_count": canonical.trade_count,
                "taker_buy_volume": float(canonical.taker_buy_volume),
                "vwap": (
                    float(canonical.vwap)
                    if canonical.vwap is not None
                    else float("nan")
                ),
                "data_quality": "ok",
                "is_closed": True,
                "timeframe": self.config.timeframe,
                "symbol": self.config.symbol,
                "candle_source": "canonical_tick_lake",
            }
            for name, value in values.items():
                self.candles.at[index, name] = value

    # --- Feed-continuity guard (gap + stall → heal or fail closed) ----------------
    def _candle_gap_bars(self, incoming_ms: int) -> int:
        """Bars missing between our last candle and the incoming one.

        0 = contiguous, N>0 = N skipped bars, -1 = backward/duplicate.
        """
        if self._tf_ms is None or not len(self.candles):
            return 0
        last_ms = int(self.candles["timestamp"].iloc[-1].value // 1_000_000)  # ns→ms
        if incoming_ms <= last_ms:
            return -1
        return (incoming_ms - last_ms) // self._tf_ms - 1

    def _enter_degraded(self, reason: str, *, recoverable: bool) -> None:
        """Put the lane in reduce-only: NEW entries blocked, exits keep running.

        A standing non-recoverable degrade (a real hole in the series) is never
        downgraded to recoverable by a later transient stall.
        """
        if self._degraded_reason is not None and not self._degraded_recoverable:
            return
        if self._degraded_reason == reason:
            return
        self._degraded_reason = reason
        self._degraded_recoverable = recoverable
        if not recoverable:
            self._recovery_bars_remaining = 0
        logger.error("lane %s DEGRADED → reduce-only: %s", self.config.symbol, reason)
        self.journal.append("lane_degraded", {
            "symbol": self.config.symbol, "reason": reason, "recoverable": recoverable,
        })

    def _clear_degraded(self, note: str) -> None:
        if self._degraded_reason is None:
            return
        logger.info("lane %s recovered from reduce-only (%s → %s)",
                    self.config.symbol, self._degraded_reason, note)
        self.journal.append("lane_recovered", {
            "symbol": self.config.symbol, "was": self._degraded_reason, "note": note,
        })
        self._degraded_reason = None
        self._degraded_recoverable = False
        self._recovery_bars_remaining = 0

    def _flag_discontinuity(self, raw_row: list) -> None:
        """Soft observability: incoming open vs our last close (no halt)."""
        if not len(self.candles):
            return
        prev_close = float(self.candles["close"].iloc[-1])
        inc_open = float(raw_row[1])
        if prev_close > 0 and abs(inc_open - prev_close) / prev_close > self._CONTINUITY_TOL:
            self.discontinuity_events += 1

    async def _gap_fill(self, since_ms: int, until_ms: int) -> list[list] | None:
        """Best-effort REST backfill of the missing [since, until) bars."""
        try:
            from vnedge.data.ccxt_client import CcxtPublicClient

            async with CcxtPublicClient(self.feed.exchange_id) as rest:
                rows = await rest.fetch_candles(
                    self.config.symbol, self.config.timeframe, since_ms, until_ms
                )
            return rows or None
        except Exception as exc:  # noqa: BLE001 — heal is best-effort; failure ⇒ fail closed
            logger.warning("gap-fill fetch failed for %s: %s", self.config.symbol, exc)
            return None

    async def _guard_candle_continuity(self, raw_row: list, now: datetime) -> bool:
        """Detect a time gap before the incoming closed bar; heal or fail closed.

        A WS reconnect can silently skip closed bars — the forward-only append
        gate would accept the jump and the contiguous-index indicators (EMA,
        ATR, close_z, rolling_percentile) would compute across the hole. Here we
        REST-backfill the missing bars (deterministic heal) or, if that fails,
        go reduce-only (block new entries; the exit stack keeps running).
        """
        incoming_ms = int(raw_row[0])
        last_ms = (
            int(self.candles["timestamp"].iloc[-1].value // 1_000_000)
            if len(self.candles)
            else None
        )
        # timestamp invariant: an open-time closed bar closed at open+tf, so it
        # must not claim to close in the future (beyond a 5s skew tolerance).
        if self._tf_ms is not None and incoming_ms + self._tf_ms > now.timestamp() * 1000 + 5000:
            self.future_candles += 1
            claimed_close = pd.to_datetime(
                incoming_ms + self._tf_ms, unit="ms", utc=True
            ).to_pydatetime()
            detail = f"candle close {claimed_close.isoformat()} is ahead of wall clock"
            self._persist_gap(GapRecord(
                self.config.symbol,
                getattr(self.feed, "exchange_id", "unknown"),
                GapKind.CLOCK_SKEW,
                now,
                claimed_close,
                now,
                detail,
            ))
            self._enter_degraded("future_candle:clock_skew", recoverable=False)
            logger.error("%s; candle withheld", detail)
            return False

        if last_ms is not None and incoming_ms < last_ms:
            incoming = pd.to_datetime(incoming_ms, unit="ms", utc=True).to_pydatetime()
            previous = pd.to_datetime(last_ms, unit="ms", utc=True).to_pydatetime()
            detail = f"closed candle moved backward: {incoming.isoformat()} < {previous.isoformat()}"
            self._persist_gap(GapRecord(
                self.config.symbol,
                getattr(self.feed, "exchange_id", "unknown"),
                GapKind.OUT_OF_ORDER,
                incoming,
                previous,
                now,
                detail,
            ))
            self._enter_degraded("out_of_order_candle", recoverable=False)
            logger.error("%s; candle withheld", detail)
            return False

        gap = self._candle_gap_bars(incoming_ms)
        if gap <= 0:
            # contiguous (or backward, which append() drops): a clean bar clears
            # a transient (recoverable) stall degrade — but never a real hole.
            if self._degraded_reason is not None and self._degraded_recoverable:
                if self._recovery_bars_remaining > 0:
                    self._recovery_bars_remaining -= 1
                    if self._recovery_bars_remaining == 0:
                        self._clear_degraded("backfill_plus_two_clean_bars")
                else:
                    self._clear_degraded("bars_resumed")
            self._flag_discontinuity(raw_row)
            return True
        self.gapped_candles += 1
        assert last_ms is not None and self._tf_ms is not None
        gap_start = pd.to_datetime(
            last_ms + self._tf_ms, unit="ms", utc=True
        ).to_pydatetime()
        gap_end = pd.to_datetime(incoming_ms, unit="ms", utc=True).to_pydatetime()
        gap_record = GapRecord(
            self.config.symbol,
            getattr(self.feed, "exchange_id", "unknown"),
            GapKind.STORAGE_HOLE,
            gap_start,
            gap_end,
            now,
            f"missing {gap} closed {self.config.timeframe} bar(s)",
        )
        self._persist_gap(gap_record)
        logger.error("feed gap: %d missing %s bar(s) before %s",
                     gap, self.config.timeframe, raw_row[0])
        if gap > self._MAX_GAP_FILL_BARS:
            self._enter_degraded(f"feed_gap:{gap}_bars_too_large", recoverable=False)
        else:
            filled = await self._gap_fill(last_ms + self._tf_ms, incoming_ms)
            expected = set(range(last_ms + self._tf_ms, incoming_ms, self._tf_ms))
            by_open = {
                int(row[0]): row
                for row in (filled or [])
                if int(row[0]) in expected
            }
            if set(by_open) == expected:
                added = sum(
                    1 for ts in sorted(by_open) if self._append_candle(by_open[ts])
                )
                self.gap_fills += 1
                logger.info("gap healed: backfilled %d/%d %s bar(s) via REST",
                            added, gap, self.config.timeframe)
                self._persist_gap(replace(
                    gap_record,
                    recovered=True,
                    detail=f"{gap_record.detail}; REST backfill proved continuity",
                ))
                # The incoming bar counts as the first clean bar. Keep entries
                # blocked until one subsequent contiguous close confirms the
                # recovered stream did not immediately break again.
                self._enter_degraded("feed_gap:recovery_warmup", recoverable=True)
                self._recovery_bars_remaining = 1
            else:
                missing = len(expected - set(by_open))
                self._persist_gap(GapRecord(
                    self.config.symbol,
                    getattr(self.feed, "exchange_id", "unknown"),
                    GapKind.BACKFILL_FAIL,
                    gap_start,
                    gap_end,
                    now,
                    f"REST backfill missing {missing}/{gap} expected bar(s)",
                ))
                self._enter_degraded(f"feed_gap:{gap}_bars_unfilled", recoverable=False)
        self._flag_discontinuity(raw_row)
        return True

    # --- Time Machine feed (read-only observability, fail-closed) -----------------
    def _tm_kline(self, row: list, now: datetime) -> dict:
        ts = pd.to_datetime(int(row[0]), unit="ms", utc=True).to_pydatetime()
        return {
            "open_time": ts, "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
            "exchange_ts": now,
        }

    def _feed_time_machine(
        self,
        now: datetime,
        closed_row: list | None = None,
        *,
        live_mid: float | None = None,
    ) -> None:
        """Feed the Time Machine. FAIL-CLOSED: never raises into the run loop.

        A Time Machine fault marks its health degraded (surfaced in the snapshot)
        but trading continues untouched under the existing feed guards.
        """
        if self.time_machine is None:
            return
        try:
            sym, tf = self.config.symbol, self.config.timeframe
            if closed_row is not None:
                self.time_machine.on_kline_update(sym, tf, self._tm_kline(closed_row, now), is_closed=True)
            forming = getattr(self.feed, "forming_candle", None)
            if forming:
                forming_row = list(forming)
                if live_mid is not None and live_mid > 0:
                    # Quote-acceptance lanes can receive thousands of BBO
                    # updates between exchange OHLCV events.  Fold the
                    # executable midpoint into the read-only forming view so
                    # Time Machine freshness follows real market evidence,
                    # not the much slower kline publisher.  Volume remains the
                    # exchange-reported value; only H/L/C are advanced.
                    forming_row[2] = max(float(forming_row[2]), live_mid)
                    forming_row[3] = min(float(forming_row[3]), live_mid)
                    forming_row[4] = live_mid
                self.time_machine.on_kline_update(
                    sym,
                    tf,
                    self._tm_kline(forming_row, now),
                    is_closed=False,
                )
            self.time_machine.check_health(now)
            self._tm_degraded = False
        except Exception as exc:  # noqa: BLE001 — observability must NEVER affect trading
            self._tm_degraded = True
            logger.warning("time machine update failed (observability only): %s", exc)

    def _tm_snapshot(self) -> dict | None:
        if self.time_machine is None:
            return None
        try:
            d = self.time_machine.snapshot_dict(self.config.symbol, now=datetime.now(UTC))
            d["degraded"] = self._tm_degraded
            return d
        except Exception:  # noqa: BLE001
            return {"degraded": True}

    def _candle_path_arm_block(self, now: datetime) -> str | None:
        """Composite candle-path arm-gate: block a NEW entry (never an exit) when
        the *decision* timeframe's Time Machine state is unsafe to arm on —
        health != ok (stale / gapped / future), the last-update age breaches the
        shared HARD budget, or the Time Machine itself faulted.

        Returns the coarse block reason, or None to allow.  Any unreadable state
        blocks arming a new position; exits bypass this gate entirely.
        """
        # A hard processing/compute breach is unsafe even if the candle values
        # themselves are healthy. A p95 needs 20 samples before it can gate;
        # immature samples remain visible to operators without halting arms.
        latency = getattr(self, "latency", None)
        if latency is not None:
            bar_stats = latency.stats(BAR_CLOSE_PROCESSING_MS) or latency.stats(
                "feed_lag_ms"
            )
            if LT.blocks_new_arms(
                LT.classify_latency_stats(
                    bar_stats,
                    soft_ms=LT.CLOSED_BAR_LAG_SOFT_P99_MS,
                    hard_ms=LT.CLOSED_BAR_LAG_HARD_P99_MS,
                    recovery_ms=LT.CLOSED_BAR_LAG_RECOVERY_MS,
                )
            ):
                return "bar_close_lag_hard"
            decision_stats = latency.stats(DECISION_LAG_MS)
            if LT.blocks_new_arms(
                LT.classify_latency_stats(
                    decision_stats,
                    soft_ms=LT.DECISION_COMPUTE_SOFT_P99_MS,
                    hard_ms=LT.DECISION_COMPUTE_HARD_P99_MS,
                    recovery_ms=LT.DECISION_COMPUTE_RECOVERY_MS,
                )
            ):
                return "decision_compute_hard"

        tm = self.time_machine
        if tm is None:
            return None
        try:
            if self._tm_degraded:
                return "tm_error"
            tf = self.config.timeframe
            health = tm.health_of(self.config.symbol, tf)
            if health != "ok":
                return f"decision_tf_{health}"
            hard = LT.TM_AGE_HARD_LAST_MS.get(tf)
            age = tm.age_ms(self.config.symbol, tf, now)
            if hard is not None and age is not None and age > hard:
                return "tm_age_hard"
        except Exception as exc:  # noqa: BLE001 — unknown state must fail closed
            logger.error("time machine arm check failed — blocking entry: %s", exc)
            return "tm_error"
        return None

    def _latency_recovery_snapshot(self) -> dict[str, dict[str, object]]:
        """Operator-visible proof behind automatic latency recovery."""
        bar_stats = self.latency.stats(BAR_CLOSE_PROCESSING_MS) or self.latency.stats(
            "feed_lag_ms"
        )
        return {
            "bar_close_processing_ms": LT.latency_recovery_state(
                bar_stats,
                soft_ms=LT.CLOSED_BAR_LAG_SOFT_P99_MS,
                hard_ms=LT.CLOSED_BAR_LAG_HARD_P99_MS,
                recovery_ms=LT.CLOSED_BAR_LAG_RECOVERY_MS,
            ),
            "decision_lag_ms": LT.latency_recovery_state(
                self.latency.stats(DECISION_LAG_MS),
                soft_ms=LT.DECISION_COMPUTE_SOFT_P99_MS,
                hard_ms=LT.DECISION_COMPUTE_HARD_P99_MS,
                recovery_ms=LT.DECISION_COMPUTE_RECOVERY_MS,
            ),
        }

    def _record_overlays(self, df: pd.DataFrame, idx: int, sig: SignalIntent | None) -> None:
        """OBSERVE-ONLY (D-lite): record what regime_v0 and the cost-aware plan
        contract WOULD say for this decision bar. Never changes the live decision
        (the classic path already produced ``sig``). Fail-safe: any error here is
        swallowed so an overlay bug can never touch trading."""
        try:
            row = df.iloc[idx]
            reading = self._regime_model.read_row(row)   # reads regime cols already on df
            self._overlay_regime = reading.to_dict()
            if sig is None:
                self._overlay_plan = None
                return
            allowed = reading.allow_long if sig.side == "long" else reading.allow_short
            if not allowed:
                self._regime_would_block += 1
            ref = float(row["close"])
            plan = signal_intent_to_plan(
                sig, ref, self.cost_model,
                decision_tf=self.config.timeframe,
                time_stop_bars=self.config.max_holding_bars,
                source=self.strategy.strategy_id,
            )
            if plan is None:
                self._overlay_plan = None
                return
            ok, reasons = plan_gate(plan, self.cost_model)
            if not ok:
                self._plan_gate_rejects += 1
            self._overlay_plan = {
                "side": plan.side, "profile": self.cost_model.profile,
                "stop_bps": round(plan.risk.stop_bps, 1),
                "tp1_bps": round(plan.tp1_bps, 1),
                "expected_net_bps": round(plan.ai.expected_net_bps, 1),
                "round_trip_bps": round(plan.costs.round_trip_bps, 1),
                "gate_ok": ok, "gate_reasons": reasons,
                "regime_allows": allowed, "regime_label": reading.label,
            }
        except Exception as exc:  # noqa: BLE001 — observability must never affect trading
            logger.debug("overlay recording failed (observability only): %s", exc)

    def _drawdown_pct(self) -> float:
        """Current drawdown from the (persisted) equity peak, in percent."""
        peak = self.tracker.peak_equity_usd
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - self.tracker.equity_usd()) / peak * 100.0)

    def _trial_days_elapsed(self, t: dict) -> int | None:
        started = t.get("started")
        if not started:                                   # fall back to trailing YYYYMMDD in the id
            tail = str(t.get("trial_id") or "")[-8:]
            started = f"{tail[:4]}-{tail[4:6]}-{tail[6:]}" if tail.isdigit() else None
        if not started:
            return None
        try:
            return (datetime.now(UTC).date() - datetime.fromisoformat(str(started)).date()).days
        except ValueError:
            return None

    def _trial_scorecard(self) -> dict | None:
        """Live pass/fail of the governed paper trial's locked criteria. A HARD
        criterion (drawdown, daily loss) failing ⇒ FAIL; an unmet accumulation
        criterion (trades, days) ⇒ PENDING; all met ⇒ PASS. Observe-only."""
        t = self.trial_meta or {}
        if not t.get("trial_id"):
            return None
        crit: list[dict] = []

        def add(name, value, threshold, ok, hard, unit=""):
            crit.append({"name": name, "value": value, "threshold": threshold,
                         "ok": bool(ok), "hard": hard, "unit": unit})

        dd = self._drawdown_pct()
        max_dd = t.get("max_dd_pct")
        add("max_drawdown", round(dd, 2), max_dd, (dd <= max_dd) if max_dd is not None else True, True, "%")
        trades = (self.fill_ledger.records // 2) if self.fill_ledger is not None else 0
        min_trades = t.get("min_trades")
        add("min_trades", trades, min_trades, (trades >= min_trades) if min_trades is not None else True, False)
        days = self._trial_days_elapsed(t)
        min_days = t.get("min_days")
        if days is not None and min_days is not None:
            add("min_days", days, min_days, days >= min_days, False, "d")
        stop = t.get("daily_stop_usd")
        if stop is not None:
            daily = self.tracker.account_state().daily_pnl_usd
            add("daily_loss", round(daily, 2), -abs(stop), daily >= -abs(stop), True, "$")

        hard_fail = any(c["hard"] and not c["ok"] for c in crit)
        pending = any((not c["hard"]) and not c["ok"] for c in crit)
        verdict = "FAIL" if hard_fail else ("PENDING" if pending else "PASS")
        return {"trial_id": t.get("trial_id"), "verdict": verdict, "criteria": crit}

    def _log_trade_event(self, event: str, detail: str, now: datetime) -> None:
        self.trade_log.append({
            "ts": now.isoformat(), "event": event, "detail": detail,
        })

    def _mode_label(self) -> str:
        if self.config.mode is RunnerMode.SHADOW:
            return "shadow (live data)"
        return "paper (live data)"

    _RUNNER_HEARTBEAT_SECONDS = 60.0
    # Bars a maker entry limit rests before it is cancelled (touch-to-fill TTL).
    _MAKER_ENTRY_TTL_BARS = 2
    # Feed-continuity guard tuning:
    _STALL_BARS = 2.5           # no closed bar in > this × timeframe ⇒ feed stalled
    _MAX_GAP_FILL_BARS = 240    # gaps larger than this fail closed instead of backfilling
    _CONTINUITY_TOL = 0.01      # |open−prev_close|/prev_close above this is logged (soft)

    def _why_no_trade(self, reason: str) -> str:
        if self._plan is not None:
            return "position_open: managing exit plan"
        if self.feed.quote is None:
            return "waiting_for_quote"
        if len(self.candles) <= self.strategy.warmup_bars:
            return "warming_up: not enough closed candles"
        if self.last_eval:
            skip = self.last_eval.get("skip_reason")
            if skip:
                return str(skip)
            if self.last_eval.get("fired"):
                if self.sizing_skips > 0 and self.orders_submitted == 0:
                    return "last_signal_rejected_by_sizing"
                if self.risk_rejects > 0 and self.orders_submitted == 0:
                    return "last_signal_rejected_by_gateway"
                return "last_eval_fired: awaiting order/fill/reconciliation state"
            return "last_eval_no_signal"
        return reason

    def _record_runner_heartbeat(
        self, reason: str, now: datetime, *, force: bool = False
    ) -> None:
        if (
            not force
            and self._last_heartbeat_at is not None
            and (now - self._last_heartbeat_at).total_seconds() < self._RUNNER_HEARTBEAT_SECONDS
        ):
            return
        self._last_heartbeat_at = now
        last_bar_ts = None
        if len(self.candles):
            last_bar_ts = self.candles["timestamp"].iloc[-1].isoformat()
        self.journal.append("paper_lane_heartbeat", {
            "reason": reason,
            "why_no_trade": self._why_no_trade(reason),
            "started_at": self._started_at.isoformat(),
            "strategy_id": self.strategy.strategy_id,
            "exchange": getattr(self.feed, "exchange_id", ""),
            "symbol": self.config.symbol,
            "timeframe": self.config.timeframe,
            "mode": self.config.mode.value,
            "runner_state": "in_position" if self._plan is not None else "waiting",
            "bars_processed": self.bars_processed,
            "evals": self.evals,
            "live_evals": self.live_evals,
            "backfill_evals": self.backfill_evals,
            "signals": self.signals,
            "live_signals": self.live_signals,
            "backfill_signals": self.backfill_signals,
            "orders_submitted": self.orders_submitted,
            "risk_rejects": self.risk_rejects,
            "sizing_skips": self.sizing_skips,
            "shadow_approved": self.shadow_approved,
            "shadow_rejected": self.shadow_rejected,
            "recon_mismatches": self.recon_mismatches,
            "dropped_candles": self.dropped_candles,
            # feed-continuity guard: reduce-only reason (or None) + counters
            "degraded": self._degraded_reason,
            "gapped_candles": self.gapped_candles,
            "gap_fills": self.gap_fills,
            "discontinuity_events": self.discontinuity_events,
            "future_candles": self.future_candles,
            "quote_seen": self.feed.quote is not None,
            "feed_staleness_seconds": float(self.feed.staleness_seconds()),
            # candle->signal pipeline latency (offline trail for the dashboard)
            "latency": self.latency.snapshot(),
            "latency_recovery": self._latency_recovery_snapshot(),
            "last_bar_ts": last_bar_ts,
            "last_eval": self.last_eval,
            "trial_id": (self.trial_meta or {}).get("trial_id"),
            "journal_path": str(self.journal.path),
            "daily_factory": self._daily_factory_payload(now),
        })

    def _roll_daily_factory(self, clock: datetime) -> None:
        if not self.config.daily_factory.enabled:
            return
        day = session_day(clock, self.config.daily_factory)
        if day == self._factory_day:
            return
        self._factory_day = day
        self._factory_entries_today = 0
        self._factory_flatten_sent = False
        self.journal.append("daily_factory_day_started", {
            "day": str(day),
            "timezone": self.config.daily_factory.session_timezone,
            "strategy_id": self.strategy.strategy_id,
            "symbol": self.config.symbol,
            "mode": self.config.mode.value,
        })

    def _daily_factory_payload(self, clock: datetime) -> dict:
        cfg = self.config.daily_factory
        payload = {
            "enabled": cfg.enabled,
            "timezone": cfg.session_timezone,
            "entries_today": self._factory_entries_today,
            "max_entries_per_day": cfg.max_entries_per_day,
            "entry_cutoff_minute": cfg.entry_cutoff_minute,
            "force_flatten_minute": cfg.force_flatten_minute,
            "daily_profit_target_usd": cfg.daily_profit_target_usd,
            "day": str(session_day(clock, cfg)),
            "flatten_sent": self._factory_flatten_sent,
        }
        payload["entry_block_reason"] = self._daily_factory_entry_block_reason(clock)
        payload["force_flatten_due"] = should_force_flatten(clock, cfg)
        return payload

    def _daily_factory_entry_block_reason(self, clock: datetime) -> str | None:
        self._roll_daily_factory(clock)
        return entry_block_reason(
            now=clock,
            config=self.config.daily_factory,
            entries_today=self._factory_entries_today,
            daily_pnl_usd=self.tracker.account_state().daily_pnl_usd,
        )

    def _new_plan(self, sig: SignalIntent, entry_bar_ts) -> _LivePlan:
        return _LivePlan(
            signal=sig,
            entry_bar_ts=pd.Timestamp(entry_bar_ts),
            exit_state=ExitEngine.from_signal(
                sig,
                config=self._current_exit_config(),
            ).state,
        )

    def _current_exit_config(self) -> ExitEngineConfig:
        config = self.config
        return ExitEngineConfig(
            trail_atr_mult=config.trail_atr_mult,
            trail_atr_window=config.trail_atr_window,
            max_holding_bars=config.max_holding_bars,
            tick_stops_enabled=config.tick_stops_enabled,
            allow_partial_tp=config.allow_partial_tp,
            fee_aware_breakeven_bps=config.fee_aware_breakeven_bps,
        )

    def _exit_engine(self, plan: _LivePlan | None = None) -> ExitEngine:
        active = plan or self._plan
        if active is None or active.exit_state is None:
            raise RuntimeError("exit engine requested without an active plan")
        return ExitEngine(active.exit_state, self._current_exit_config())

    def _trail_atr(self) -> float:
        """Canonical ATR for the trail — the SAME indicator+window the backtester
        uses, so research and runtime trail identically. 0.0 if not warmed."""
        if self.config.trail_atr_mult <= 0.0 or len(self.candles) < 2:
            return 0.0
        try:
            series = _atr_indicator(self.candles, self.config.trail_atr_window)
        except Exception:  # noqa: BLE001 - trailing must never break the exit loop
            self._trail_atr_faults += 1     # surfaced in the snapshot so a stuck trail is visible
            return 0.0
        value = float(series.iloc[-1])
        return value if math.isfinite(value) else 0.0

    def _shadow_strategy_exit(
        self,
        side: str,
        entry_price: float,
        bar: pd.Series,
    ) -> StrategyExitIntent | None:
        df = self._shadow_exit_df
        if df is None or df.empty:
            return None
        try:
            index = int(bar.name)
        except (TypeError, ValueError):
            index = len(df) - 1
        if index < 0 or index >= len(df):
            index = len(df) - 1
        return self.strategy.exit_signal(df, index, side, entry_price)

    def _seed_plan_from_venue(self, plan: _LivePlan, client_order_id: str | None = None) -> None:
        if client_order_id is not None:
            status = self.exchange.get_order_status(client_order_id)
            if status is not None and status.filled_qty > 0:
                plan.exit_state.seed_entry(
                    entry_price=status.avg_fill_price,
                    quantity=status.filled_qty,
                )
                return
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        if pos is not None:
            plan.exit_state.seed_entry(
                entry_price=pos.entry_price,
                quantity=abs(pos.quantity),
            )

    def _current_position_quantity(self) -> float:
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        return abs(pos.quantity) if pos is not None else 0.0

    async def _submit_entry(self, sig: SignalIntent, now: datetime) -> None:
        decision_bar_ts = self.candles["timestamp"].iloc[-1]
        decision_price = float(self.candles["close"].iloc[-1])
        scanner_context = {
            "strategy_id": self.strategy.strategy_id,
            "symbol": self.config.symbol,
            "timeframe": self.config.timeframe,
            "side": sig.side,
            "signal_reason": sig.reason,
            "bar_ts": decision_bar_ts.isoformat(),
            "decision_price": decision_price,
            "stop_price": sig.stop_price,
            "take_profit_price": sig.take_profit_price,
        }
        # A shadow intent is a real reservation in the virtual book even
        # though it never reaches an exchange.  Treat it exactly like an open
        # plan for entry concurrency: otherwise every later signal can stack a
        # second notional on the same purse while PortfolioTracker still shows
        # no position.  Keep this guard here (rather than only in ``run``) so a
        # future/direct caller cannot bypass the single-book invariant.
        if (
            self.config.mode is RunnerMode.SHADOW
            and self.shadow_outcomes is not None
            and self.shadow_outcomes.has_pending
        ):
            self.last_reject_reason = "shadow_book: unresolved virtual position"
            self.journal.append(
                "shadow_entry_blocked",
                {**scanner_context, "reason": self.last_reject_reason},
            )
            self._log_trade_event(
                "shadow_entry_blocked",
                f"{sig.side} — unresolved virtual position already reserves the purse"[:140],
                now,
            )
            return
        bid, ask = self.feed.quote
        ref_price = ask if sig.side == "long" else bid
        targets = [
            float(target)
            for target in (sig.take_profit_price, *sig.take_profit_levels)
            if target is not None and math.isfinite(float(target))
        ]
        favorable = [
            target
            for target in targets
            if (sig.side == "long" and target > ref_price)
            or (sig.side == "short" and target < ref_price)
        ]
        if not favorable:
            self.last_reject_reason = "cost_gate: no favorable target/edge hypothesis"
            self.journal.append("cost_rejected", {
                **scanner_context,
                "reason": self.last_reject_reason,
            })
            self._log_trade_event("cost_rejected", self.last_reject_reason, now)
            return
        target = max(favorable) if sig.side == "long" else min(favorable)
        signal_edge_bps = (
            (target - ref_price) / ref_price * 10_000.0
            if sig.side == "long"
            else (ref_price - target) / ref_price * 10_000.0
        )
        maker = (
            self.config.mode is not RunnerMode.SHADOW
            and is_maker_route_strategy(self.strategy.strategy_id)
        )
        cost_decision = self.entry_cost_gate.evaluate(
            signal_edge_bps=signal_edge_bps,
            side=sig.side,
            urgency="maker" if maker else "taker",
            expected_holding_seconds=(
                max(1, self.config.max_holding_bars) * (self._tf_seconds or 0)
            ),
            current_funding_rate=getattr(self.feed, "funding_rate", 0.0),
            symbol=self.config.symbol,
            available_room_bps=signal_edge_bps,
        )
        if not cost_decision.approved:
            self.last_reject_reason = f"cost_gate: {cost_decision.reason}"
            self.journal.append("cost_rejected", {
                **scanner_context,
                "signal_edge_bps": signal_edge_bps,
                "expected_net_bps": str(cost_decision.expected_net_bps),
                "total_cost_bps": str(cost_decision.cost.total_cost_bps),
                "min_required_bps": str(cost_decision.min_required_bps),
                "reason": cost_decision.reason,
            })
            self._log_trade_event("cost_rejected", self.last_reject_reason, now)
            return
        sizing = size_position(
            equity_usd=self.tracker.equity_usd(), entry_price=ref_price,
            stop_price=sig.stop_price, side=sig.side,
            config=self.config.risk, limits=self.config.limits,
        )
        if not sizing.approved:
            self.sizing_skips += 1
            self.last_reject_reason = f"sizing: {', '.join(sizing.reasons)}"
            self.journal.append(
                "sizing_rejected",
                {**scanner_context, "reason": self.last_reject_reason},
            )
            self._log_trade_event("sizing_skip", f"{sig.side} rejected by sizing: {', '.join(sizing.reasons)}"[:140], now)
            return
        # Maker-edge strategies post a passive resting limit at the near touch
        # (bid for a long, ask for a short) instead of crossing the spread — the
        # route their scorecard edge is defined on. SHADOW never places real
        # orders, so it stays market (its virtual pricing is handled separately).
        intent = OrderIntent(
            symbol=self.config.symbol, side=sig.side, quantity=sizing.quantity,
            notional_usd=sizing.notional_usd,
            leverage=max(sizing.required_leverage, 1.0),
            reduce_only=False, strategy_id=self.strategy.strategy_id,
            order_type="limit" if maker else "market",
            limit_price=(bid if sig.side == "long" else ask) if maker else None,
        )
        key = make_intent_key(
            self.strategy.strategy_id, self.config.symbol, sig.side,
            decision_bar_ts,
        )
        if self.config.mode is RunnerMode.SHADOW:
            decision = self.gateway.evaluate(
                intent, self.tracker.account_state(), self._market_state(), now=now
            )
            if decision.approved and self.shadow_portfolio is not None:
                shared = self.shadow_portfolio.evaluate_entry(
                    lane_id=str((self.trial_meta or {}).get("trial_id", "unknown")),
                    symbol=self.config.symbol,
                    side=sig.side,
                    margin_usd=Decimal(str(intent.notional_usd / intent.leverage)),
                    now=now,
                    intent_key=key,
                )
                if not shared.allowed:
                    self.shadow_rejected += 1
                    self.last_reject_reason = f"shadow_portfolio: {shared.reason}"
                    self.journal.append("shadow_portfolio_rejected", {
                        **scanner_context,
                        "reason": shared.reason,
                        "active_margin_usd": str(shared.active_margin_usd),
                        "daily_net_usd": str(shared.daily_net_usd),
                        "unresolved_intents": shared.unresolved_intents,
                    })
                    self._log_trade_event(
                        "shadow_portfolio_rejected", self.last_reject_reason, now
                    )
                    return
            self.journal.append("shadow_intent", {
                "intent_key": key,
                "approved": decision.approved,
                "failed_checks": list(decision.failed_checks),
                "passed_checks": list(decision.passed_checks),
                "explanation": decision.explanation,
                "intent": asdict(intent),
                "signal_reason": sig.reason,
                # stop/target/decision bar make the intent resolvable into a
                # virtual outcome later (and on restart, from the journal)
                "stop_price": sig.stop_price,
                "take_profit_price": sig.take_profit_price,
                "take_profit_levels": list(sig.take_profit_levels),
                "bar_ts": decision_bar_ts.isoformat(),
                "timeframe": self.config.timeframe,
                "decision_price": decision_price,
            })
            if decision.approved:
                self.shadow_approved += 1
                self._factory_entries_today += 1
                if self.shadow_outcomes is not None:
                    self.shadow_outcomes.track(
                        intent_key=key, side=sig.side,
                        quantity=intent.quantity,
                        notional_usd=intent.notional_usd,
                        stop_price=sig.stop_price,
                        take_profit_price=sig.take_profit_price,
                        decision_bar_ts=decision_bar_ts,
                        signal_reason=sig.reason,
                        take_profit_levels=sig.take_profit_levels,
                    )
                self._log_trade_event(
                    "shadow_approved",
                    f"{sig.side} {intent.quantity:g} @ ~{ref_price:g} — {sig.reason}"[:140],
                    now,
                )
            else:
                self.shadow_rejected += 1
                self.last_reject_reason = (
                    f"gateway: {', '.join(decision.failed_checks)}"
                )
                self._log_trade_event(
                    "shadow_rejected",
                    f"{sig.side} — failed: {', '.join(decision.failed_checks)}"[:140],
                    now,
                )
            return
        order = await self.om.submit(
            intent, self.tracker.account_state(), self._market_state(), key, now=now
        )
        if order.state is OrderState.RISK_REJECTED:
            self.risk_rejects += 1
            self._log_trade_event("risk_rejected", f"{sig.side} — gateway rejected entry"[:140], now)
        else:
            self.orders_submitted += 1
            self._factory_entries_today += 1
            self._log_trade_event(
                "order_submitted",
                f"{sig.side} {intent.quantity:g} ({order.state.value}) — {sig.reason}"[:140],
                now,
            )
            plan = self._new_plan(sig, self.candles["timestamp"].iloc[-1])
            self._seed_plan_from_venue(plan, order.client_order_id)
            venue_status = self.exchange.get_order_status(order.client_order_id)
            filled = venue_status is not None and venue_status.filled_qty > 0
            if maker and order.state is OrderState.ACKNOWLEDGED and not filled:
                # A resting maker limit: NOT a position until a later quote
                # touches it. Park it — a new entry can't fire while it rests.
                self._pending_entry = _PendingMakerEntry(
                    plan=plan, client_order_id=order.client_order_id
                )
            elif order.state is OrderState.ACKNOWLEDGED:
                self._plan = plan
            elif order.state is OrderState.TIMEOUT_UNKNOWN:
                self._parked_entries[order.client_order_id] = plan

    def _approve_scanner_fire(
        self, fire, bar_index: int, bar_ts: datetime
    ) -> ScannerApproval:
        """Run a scanner candidate through the normal sizing+risk boundary.

        This returns data to the read-only scanner runner; it never calls
        OrderManager and therefore cannot submit an order. The same central
        gateway still decides whether the virtual intent is admissible.
        """
        if self.config.mode is not RunnerMode.SHADOW:
            return ScannerApproval(
                approved=False,
                intent={},
                failed_checks=("scanner_shadow_only",),
                explanation="canonical scanner runner has no paper/live authority",
            )
        if self.feed.quote is None:
            return ScannerApproval(
                approved=False,
                intent={},
                failed_checks=("missing_quote",),
                explanation="scanner fire has no executable reference quote",
            )
        cp_block = self._candle_path_arm_block(datetime.now(UTC))
        factory_block = self._daily_factory_entry_block_reason(bar_ts)
        protected, protection_reason = self.protections.entries_allowed(bar_index)
        local_failure = (
            f"candle_path:{cp_block}" if cp_block is not None
            else factory_block
            if factory_block is not None
            else protection_reason
            if not protected
            else None
        )
        if local_failure is not None:
            return ScannerApproval(
                approved=False,
                intent={},
                failed_checks=(local_failure,),
                explanation=local_failure,
            )

        bid, ask = self.feed.quote
        ref_price = ask if fire.side == "long" else bid
        risk_bps = abs(ref_price - float(fire.stop)) / ref_price * 10_000.0
        features = getattr(self.strategy, "_features", None)
        reward_r = float(getattr(getattr(features, "params", None), "reward_r", 2.0))
        signal_edge_bps = risk_bps * reward_r
        cost_decision = self.entry_cost_gate.evaluate(
            signal_edge_bps=signal_edge_bps,
            side=fire.side,
            urgency="taker",
            expected_holding_seconds=(
                max(1, self.config.max_holding_bars) * (self._tf_seconds or 0)
            ),
            current_funding_rate=getattr(self.feed, "funding_rate", 0.0),
            symbol=self.config.symbol,
            available_room_bps=signal_edge_bps,
        )
        if not cost_decision.approved:
            return ScannerApproval(
                approved=False,
                intent={},
                failed_checks=(f"cost_gate:{cost_decision.reason}",),
                explanation=cost_decision.reason or "cost gate rejected scanner candidate",
            )
        sizing = size_position(
            equity_usd=self.tracker.equity_usd(),
            entry_price=ref_price,
            stop_price=fire.stop,
            side=fire.side,
            config=self.config.risk,
            limits=self.config.limits,
        )
        if not sizing.approved:
            self.sizing_skips += 1
            reasons = tuple(f"sizing:{reason}" for reason in sizing.reasons)
            return ScannerApproval(
                approved=False,
                intent={},
                failed_checks=reasons,
                explanation=", ".join(reasons),
            )
        leverage = max(sizing.required_leverage, 1.0)
        intent = OrderIntent(
            symbol=self.config.symbol,
            side=fire.side,
            quantity=sizing.quantity,
            notional_usd=sizing.notional_usd,
            leverage=leverage,
            reduce_only=False,
            strategy_id=self.strategy.strategy_id,
            order_type="market",
        )
        decision = self.gateway.evaluate(
            intent,
            self.tracker.account_state(),
            self._market_state(),
            now=datetime.now(UTC),
        )
        key_prefix = {
            "squeeze_expansion_breakout_v2": "squeeze_observe",
            "squeeze_expansion_breakout_v3": "squeeze_acceptance_v3",
            "squeeze_expansion_breakout_v4": "squeeze_expansion_breakout_v4",
        }.get(self.strategy.strategy_id, self.strategy.strategy_id)
        intent_key = (
            f"{key_prefix}|{self.config.symbol}|{fire.side}|"
            f"{int(bar_ts.timestamp() * 1000)}"
        )
        if decision.approved and self.shadow_portfolio is not None:
            shared = self.shadow_portfolio.evaluate_entry(
                lane_id=str((self.trial_meta or {}).get("trial_id", "unknown")),
                symbol=self.config.symbol,
                side=fire.side,
                margin_usd=Decimal(str(intent.notional_usd / leverage)),
                now=bar_ts,
                intent_key=intent_key,
            )
            if not shared.allowed:
                return ScannerApproval(
                    approved=False,
                    intent=asdict(intent),
                    failed_checks=(f"shadow_portfolio:{shared.reason}",),
                    passed_checks=tuple(decision.passed_checks),
                    explanation=shared.reason,
                    notional_usd=intent.notional_usd,
                    margin_usd=intent.notional_usd / leverage,
                    intent_key=intent_key,
                )
        return ScannerApproval(
            approved=decision.approved,
            intent=asdict(intent),
            failed_checks=tuple(decision.failed_checks),
            passed_checks=tuple(decision.passed_checks),
            explanation=decision.explanation,
            notional_usd=intent.notional_usd,
            margin_usd=intent.notional_usd / leverage,
            intent_key=intent_key,
        )

    async def _manage_pending_entry(self, now: datetime) -> None:
        """Resolve a resting maker entry: promote to a position once a quote has
        touched it (checked after the bar's quote sync), or cancel it — skipping
        the trade — once its touch-to-fill TTL lapses without a fill."""
        pending = self._pending_entry
        if pending is None:
            return
        status = self.exchange.get_order_status(pending.client_order_id)
        if status is not None and status.filled_qty > 0:
            self._pending_entry = None
            pending.plan.exit_state.seed_entry(
                entry_price=status.avg_fill_price,
                quantity=status.filled_qty,
            )
            self._plan = pending.plan
            self._log_trade_event(
                "maker_entry_filled",
                f"{pending.plan.signal.side} filled @ {status.avg_fill_price:g} (maker)"[:140],
                now,
            )
            return
        pending.bars_waited += 1
        if pending.bars_waited >= self._MAKER_ENTRY_TTL_BARS:
            await self.om.cancel_order(
                pending.client_order_id, reason="maker entry TTL — no touch"
            )
            self._pending_entry = None
            self._log_trade_event(
                "maker_entry_unfilled",
                f"{pending.plan.signal.side} no touch in {pending.bars_waited} bars — skipped"[:140],
                now,
            )

    async def _cancel_pending_entry_for_daily_factory(
        self, clock: datetime, now: datetime
    ) -> None:
        if self._pending_entry is None:
            return
        cfg = self.config.daily_factory
        if not cfg.enabled or not cfg.cancel_resting_entries_at_cutoff:
            return
        if self._daily_factory_entry_block_reason(clock) is None:
            return
        pending = self._pending_entry
        await self.om.cancel_order(
            pending.client_order_id,
            reason="daily factory cutoff — resting entry cancelled",
        )
        self._pending_entry = None
        self.journal.append("daily_factory_pending_entry_cancelled", {
            "client_order_id": pending.client_order_id,
            "strategy_id": self.strategy.strategy_id,
            "symbol": self.config.symbol,
            "clock": clock.isoformat(),
        })
        self._log_trade_event(
            "daily_factory_cancel",
            f"{pending.plan.signal.side} maker entry cancelled at daily cutoff"[:140],
            now,
        )

    async def _enforce_daily_factory_flatten(
        self, clock: datetime, now: datetime
    ) -> None:
        cfg = self.config.daily_factory
        if not should_force_flatten(clock, cfg) or self._factory_flatten_sent:
            return
        await self._cancel_pending_entry_for_daily_factory(clock, now)
        if not self.exchange.get_positions():
            self._factory_flatten_sent = True
            return
        # One deterministic exit key per session day. If a prior submission is
        # unresolved, _submit_exit preserves the plan and avoids double-submit.
        key_ts = int(pd.Timestamp(clock.date()).value)
        pre_plan = self._plan
        pre_state = pre_plan.exit_state if pre_plan is not None else None
        pre_sig = pre_plan.signal if pre_plan is not None else None
        order = await self._submit_exit(
            "daily_factory_close",
            key_ts,
            now,
            quantity=None,
            final=True,
        )
        if order is None:
            return
        if order.state in _EXIT_ACCEPTED_STATES:
            self._factory_flatten_sent = True
        self.journal.append("live_paper_exit", {
            "reason": "daily_factory_close",
            "state": order.state.value,
            "client_order_id": order.client_order_id,
            "take_profit_levels": list(pre_sig.take_profit_levels) if pre_sig else [],
            "tp_number": 0,
            "tp_reached": pre_state.tp_reached() if pre_state is not None else 0,
            "mfe_price": pre_state.mfe_price if pre_state is not None else None,
            "exit_price": None,
            "quantity": None,
            "final": True,
            "active_stop_price": pre_state.current_stop if pre_state is not None else None,
            "breakeven_armed": pre_state.breakeven_armed if pre_state is not None else False,
        })
        self.journal.append("daily_factory_flatten", {
            "state": order.state.value,
            "client_order_id": order.client_order_id,
            "strategy_id": self.strategy.strategy_id,
            "symbol": self.config.symbol,
            "clock": clock.isoformat(),
        })
        self._log_trade_event(
            "daily_factory_close",
            f"force-flat before session close ({order.state.value})"[:140],
            now,
        )

    def _max_holding_hit(self, bar: pd.Series) -> bool:
        """True once the open plan has been held for ``max_holding_bars`` closed
        bars — the SAME time cap the backtester (and shadow) enforce, so a
        paper/live position times out exactly like the config it was judged
        under. Counted statelessly from the persisted ``entry_bar_ts`` against
        the (untrimmed, resume-seeded) candle history, so it survives restarts.
        """
        return self._bars_held(bar) >= self.config.max_holding_bars

    def _bars_held(self, bar: pd.Series) -> int:
        if self._plan is None:
            return 0
        entry_ts = self._plan.entry_bar_ts
        current_ts = pd.Timestamp(bar["timestamp"])
        return int(
            (
                (self.candles["timestamp"] > entry_ts)
                & (self.candles["timestamp"] <= current_ts)
            ).sum()
        )

    async def _manage_exit(self, bar: pd.Series, now: datetime) -> None:
        if self._plan is None:
            return
        sig = self._plan.signal
        engine = self._exit_engine()
        decision = engine.on_bar(
            high=float(bar["high"]),
            low=float(bar["low"]),
            close=float(bar["close"]),
            position_quantity=self._current_position_quantity(),
            min_qty=self.config.limits.min_qty,
            qty_step=self.config.limits.qty_step,
            bars_held=self._bars_held(bar),
            atr=self._trail_atr(),
        )
        if decision is None:
            decision = self._strategy_exit_decision(bar)
        if decision is None:
            return
        levels = list(sig.take_profit_levels)
        order = await self._submit_exit(
            decision.reason,
            int(bar["timestamp"].value),
            now,
            quantity=decision.quantity,
            final=decision.final,
            decision=decision,
        )
        if order is None:
            return
        if order.state in _EXIT_ACCEPTED_STATES and self._plan is not None:
            engine.mark_fill(decision)
        self.journal.append("live_paper_exit", {
            "reason": decision.reason,
            "state": order.state.value,
            "client_order_id": order.client_order_id,
            "take_profit_levels": levels,
            "tp_number": decision.tp_number,
            "tp_reached": decision.tp_reached,
            "mfe_price": decision.mfe_price,
            "exit_price": decision.exit_price,
            "quantity": decision.quantity,
            "final": decision.final,
            "active_stop_price": decision.active_stop_price,
            "breakeven_armed": decision.breakeven_armed,
        })
        self._log_trade_event("exit", f"{decision.reason} ({order.state.value})"[:140], now)

    def _strategy_exit_decision(self, bar: pd.Series) -> ActiveExitDecision | None:
        if self._plan is None:
            return None
        df = self.strategy.prepare(self.candles).reset_index(drop=True)
        if df.empty:
            return None
        index = len(df) - 1
        sig = self._plan.signal
        entry = self._plan.exit_state.entry_price or float(bar["close"])
        exit_sig = self.strategy.exit_signal(df, index, sig.side, entry)
        if exit_sig is None:
            return None
        return self._exit_engine().on_strategy_exit(
            reason=exit_sig.reason,
            price=(
                float(exit_sig.exit_price)
                if exit_sig.exit_price is not None
                else float(bar["close"])
            ),
        )

    async def _submit_exit(
        self,
        reason: str,
        key_ts: int,
        now: datetime,
        *,
        quantity: float | None = None,
        final: bool = True,
        decision: ActiveExitDecision | None = None,
    ):
        """Shared reduce-only exit submission — the ONLY way a plan closes.

        Both the bar-close path (_manage_exit) and the tick-stop path
        (_check_tick_stop) flow through here, so every exit passes the same
        gateway/journal/OrderManager pipeline. Returns the ManagedOrder, or
        None if no position existed or an unresolved prior exit attempt is
        awaiting reconciliation. The plan is cleared only after the exit is
        accepted/filled or the position is confirmed flat."""
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        if pos is None:
            self._clear_exit_plan()
            return None
        close_qty = abs(pos.quantity) if quantity is None else min(abs(pos.quantity), quantity)
        if close_qty <= 0:
            return None
        base_key = f"exit|{self.config.symbol}|{reason}|{key_ts}"
        pending = self._pending_exit_orders.get(base_key)
        if pending is not None:
            pending_order = self.om.orders.get(pending)
            if pending_order is not None and pending_order.state in (
                OrderState.TIMEOUT_UNKNOWN,
                OrderState.RECONCILING,
            ):
                self.journal.append("exit_plan_waiting_reconciliation", {
                    "intent_key": base_key,
                    "client_order_id": pending,
                    "reason": reason,
                    "state": pending_order.state.value,
                })
                return None
            self._pending_exit_orders.pop(base_key, None)
            self._pending_exit_finals.pop(base_key, None)
            self._pending_exit_decisions.pop(base_key, None)
        intent = OrderIntent(
            symbol=self.config.symbol,
            side="short" if pos.quantity > 0 else "long",
            quantity=close_qty, notional_usd=0.0, leverage=1.0,
            reduce_only=True, strategy_id=self.strategy.strategy_id,
        )
        intent_key = self._exit_intent_key(base_key)
        order = await self.om.submit(
            intent, self.tracker.account_state(), self._market_state(),
            intent_key=intent_key,
            now=now,
        )
        self.orders_submitted += 1
        if order.state in _EXIT_ACCEPTED_STATES:
            self._mark_exit_accepted(reason, final=final)
        else:
            self._preserve_exit_plan(
                base_key, order, reason, final=final, decision=decision
            )
        return order

    def _exit_intent_key(self, base_key: str) -> str:
        attempt = self._exit_retry_attempts.get(base_key, 0)
        return base_key if attempt == 0 else f"{base_key}|retry={attempt}"

    def _mark_exit_accepted(self, reason: str, *, final: bool = True) -> None:
        if not final:
            return
        self._clear_exit_plan()
        # A tick stop fires BETWEEN closes, so the exit belongs to the bar
        # currently forming (index len(candles)): a 1-bar cooldown then blocks
        # that bar's entry evaluation, exactly as a bar-close stop blocks its
        # own bar's re-entry.
        exit_bar = len(self.candles) - (0 if reason == "tick_stop" else 1)
        self.protections.on_exit(reason, exit_bar)

    def _clear_exit_plan(self) -> None:
        self._plan = None
        self._pending_exit_orders.clear()
        self._pending_exit_finals.clear()
        self._pending_exit_decisions.clear()
        self._exit_retry_attempts.clear()

    def _preserve_exit_plan(
        self,
        base_key: str,
        order,
        reason: str,
        *,
        final: bool = True,
        decision: ActiveExitDecision | None = None,
    ) -> None:
        if order.state in (OrderState.TIMEOUT_UNKNOWN, OrderState.RECONCILING):
            self._pending_exit_orders[base_key] = order.client_order_id
            self._pending_exit_finals[base_key] = final
            if decision is not None:
                self._pending_exit_decisions[base_key] = decision
        elif order.state in _EXIT_RETRYABLE_STATES:
            self._exit_retry_attempts[base_key] = self._exit_retry_attempts.get(base_key, 0) + 1
        if order.state is OrderState.RISK_REJECTED:
            self.risk_rejects += 1
        self.journal.append("exit_plan_preserved", {
            "intent_key": base_key,
            "client_order_id": order.client_order_id,
            "reason": reason,
            "state": order.state.value,
            "next_retry": self._exit_intent_key(base_key),
        })
        logger.warning(
            "preserving exit plan after %s submit ended %s (%s)",
            reason,
            order.state.value,
            order.client_order_id,
        )

    async def _check_tick_stop(self, now: datetime) -> None:
        """Idle-tick STOP monitoring — capital protection at quote granularity.

        Between bar closes, ONLY the stop is checked against the live
        top-of-book (long: bid <= stop; short: ask >= stop — the side an exit
        would actually fill on, the same trigger convention as
        scalping.tick_stop). Take-profits deliberately stay bar-close: a stop
        is capital protection where every bar of delay is unbounded downside
        (measured 2026-07-06: a short's stop filled at 64,489 vs an intra-bar
        breach much earlier), while a TP is strategy semantics — the
        backtester models TPs at bar granularity, so tick TPs would make
        paper results diverge from research.

        Shadow lanes never hold fills/positions, so no plan is ever armed
        there and this never triggers; the explicit mode guard documents that
        and keeps it true even if a plan were ever armed by mistake.
        """
        if (
            self._plan is None
            or not self.config.tick_stops_enabled
            or self.config.mode is RunnerMode.SHADOW
            or self.feed.quote is None
        ):
            return
        sig = self._plan.signal
        entry_bar_ts = self._plan.entry_bar_ts
        bid, ask = self.feed.quote
        decision = self._exit_engine().on_tick(
            bid=bid,
            ask=ask,
            position_quantity=self._current_position_quantity(),
        )
        if decision is None:
            return
        stop_price = decision.active_stop_price
        self._sync_quote()  # exit must fill at the breach quote, not the last bar's
        # key_ts = entry bar: one tick-stop intent per plan, minted once —
        # never re-derived from the (wall-clock) breach time
        order = await self._submit_exit(
            decision.reason,
            int(entry_bar_ts.value),
            now,
            decision=decision,
        )
        if order is None:
            return
        if order.state in _EXIT_ACCEPTED_STATES:
            self.tick_stop_exits += 1
        trigger_px = bid if sig.side == "long" else ask
        self.journal.append("tick_stop_exit", {
            "reason": "tick_stop",
            "state": order.state.value,
            "client_order_id": order.client_order_id,
            "side": sig.side,
            "stop_price": stop_price,
            "initial_stop_price": sig.stop_price,
            "take_profit_price": sig.take_profit_price,
            "take_profit_levels": list(sig.take_profit_levels),
            "active_stop_price": stop_price,
            "breakeven_armed": self._plan.exit_state.breakeven_armed if self._plan else True,
            "bid": bid,
            "ask": ask,
            "entry_bar_ts": entry_bar_ts.isoformat(),
            "signal_reason": sig.reason,
        })
        self._log_trade_event(
            "exit",
            f"tick_stop {sig.side} — {'bid' if sig.side == 'long' else 'ask'} "
            f"{trigger_px:g} breached stop {stop_price:g} ({order.state.value})"[:140],
            now,
        )
        # persist immediately — a crash before the next bar must not restore
        # the already-closed position/plan
        self._ledger_new_fills(now)
        if self.account_store is not None:
            self.account_store.save_from(
                self.exchange, self.tracker, plan=self._serialize_plan()
            )

    def _maybe_daily_report(self, now: datetime) -> None:
        """At each UTC day rollover, journal a summary of the finished day
        and push it through the alert notifiers (severity info)."""
        day = now.date()
        if self._report_day is None:
            self._report_day = day
            self._day_open_equity = self.tracker.equity_usd()
            self._day_open_fills = len(self.exchange.get_fills())
            return
        if day == self._report_day:
            return
        equity = self.tracker.equity_usd()
        fills = len(self.exchange.get_fills())
        summary = (
            f"daily report {self._report_day}: equity ${equity:.2f} "
            f"({equity - self._day_open_equity:+.2f}), "
            f"fills {fills - self._day_open_fills} (total {fills}), "
            f"open positions {len(self.exchange.get_positions())}, "
            f"loss streak {self.tracker.consecutive_losses}, "
            f"risk rejects {self.risk_rejects}, recon mismatches {self.recon_mismatches}"
        )
        self.journal.append("daily_report", {"day": str(self._report_day), "summary": summary})
        if self.alert_engine is not None:
            alert = {"ts": now.isoformat(), "rule_id": "daily_report",
                     "severity": "info", "message": summary, "mode": self._mode_label()}
            self.alert_engine.recent.append(alert)
            for notifier in self.alert_engine.notifiers:
                try:
                    notifier.send(alert)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("daily report notifier failed: %s", exc)
        self._report_day = day
        self._day_open_equity = equity
        self._day_open_fills = fills

    def _serialize_plan(self) -> dict | None:
        if self._plan is None:
            return None
        sig = self._plan.signal
        return {
            "side": sig.side,
            "stop_price": sig.stop_price,
            "take_profit_price": sig.take_profit_price,
            "take_profit_levels": list(sig.take_profit_levels),
            "reason": sig.reason,
            "entry_bar_ts": self._plan.entry_bar_ts.isoformat(),
            "active_exit": self._plan.exit_state.to_dict(),
        }

    def restore_plan(self, stored: dict | None) -> None:
        """Re-arm exit management for a restored position.

        Preferred: the exact persisted plan. Legacy snapshots (no plan saved):
        ask the strategy to rebuild one with its own signal() formulas; if it
        cannot, the orphan guard keeps its manual-flatten semantics. Either
        path is journaled — a resumed trade must be as explainable as a fresh
        one."""
        positions = self.exchange.get_positions()
        if not positions or self._plan is not None:
            return
        pos = positions[0]
        if stored is not None and not self._restored_stop_sane(
            pos, stored["side"], float(stored["stop_price"])
        ):
            # corrupted/hand-edited store: refuse the plan; orphan-guard
            # manual-flatten semantics are safer than a bad stop
            self.journal.append("plan_restore_rejected", {
                "reason": "stop fails sanity bounds", "stored": dict(stored),
            })
            logger.warning("restored plan REJECTED (insane stop)")
            return
        if stored is not None:
            sig = SignalIntent(
                stored["side"], stop_price=float(stored["stop_price"]),
                take_profit_price=(float(stored["take_profit_price"])
                                   if stored.get("take_profit_price") is not None else None),
                take_profit_levels=tuple(float(x) for x in stored.get("take_profit_levels") or ()),
                reason=stored.get("reason", "restored plan"),
            )
            self._plan = self._new_plan(sig, pd.Timestamp(stored["entry_bar_ts"]))
            self._plan.exit_state.restore(stored.get("active_exit"))
            self._seed_plan_from_venue(self._plan)
            self.journal.append("plan_restored", dict(stored))
            logger.info("trade plan restored from account store: %s", sig.reason)
            return
        if len(self.candles) <= self.strategy.warmup_bars:
            return
        df = self.strategy.prepare(self.candles)
        sig = self.strategy.synthesize_exit_plan(
            df, len(df) - 1, pos.side, pos.entry_price
        )
        if sig is None:
            return  # orphan guard will handle it (entries halted, manual flatten)
        sig = self._clamp_synthesized_stop(pos, sig)
        self._plan = self._new_plan(sig, df["timestamp"].iloc[-1])
        self._seed_plan_from_venue(self._plan)
        self.journal.append("plan_rebuilt_on_resume", {
            "side": sig.side, "stop_price": sig.stop_price,
            "take_profit_price": sig.take_profit_price,
            "take_profit_levels": list(sig.take_profit_levels),
            "reason": sig.reason,
        })
        logger.info("trade plan REBUILT on resume: %s", sig.reason)

    # A synthesized stop uses CURRENT ATR; after a volatile gap it could sit
    # far wider than the original risk envelope (2026-07-09 audit finding).
    # Cap rebuilt stop distance at this fraction of entry price — well above
    # any normal 1.5-ATR distance, so it binds only in the pathological case.
    _MAX_REBUILT_STOP_PCT = 0.03

    def _restored_stop_sane(self, pos, side: str, stop: float) -> bool:
        if not math.isfinite(stop) or stop <= 0:
            return False
        entry = pos.entry_price
        if side == "long" and not stop < entry:
            return False
        if side == "short" and not stop > entry:
            return False
        return abs(stop - entry) / entry <= 3 * self._MAX_REBUILT_STOP_PCT

    def _clamp_synthesized_stop(self, pos, sig: SignalIntent) -> SignalIntent:
        entry = pos.entry_price
        max_dist = self._MAX_REBUILT_STOP_PCT * entry
        if abs(sig.stop_price - entry) <= max_dist:
            return sig
        clamped = entry - max_dist if sig.side == "long" else entry + max_dist
        self.journal.append("plan_stop_clamped", {
            "side": sig.side, "synthesized_stop": sig.stop_price,
            "clamped_stop": clamped, "entry_price": entry,
        })
        logger.warning("synthesized stop %.6g beyond %.0f%% cap — clamped to %.6g",
                       sig.stop_price, self._MAX_REBUILT_STOP_PCT * 100, clamped)
        return SignalIntent(sig.side, stop_price=clamped,
                            take_profit_price=sig.take_profit_price,
                            take_profit_levels=sig.take_profit_levels,
                            reason=sig.reason + " [stop clamped on rebuild]")

    def _guard_orphaned_position(self) -> None:
        if self._orphan_position_guarded or self._plan is not None \
                or self._pending_entry is not None or self._parked_entries:
            return
        positions = self.exchange.get_positions()
        if not positions:
            return
        self._orphan_position_guarded = True
        reason = (
            "restored paper position without active trade plan — entries halted; "
            "manual reduce-only flatten required"
        )
        self.gateway.kill_switch.activate(reason)
        self.journal.append("orphaned_paper_position", {
            "reason": reason,
            "positions": [
                {"symbol": p.symbol, "side": p.side, "quantity": abs(p.quantity)}
                for p in positions
            ],
        })

    def _fail_closed_on_reconciliation(self, mismatches: tuple[str, ...]) -> None:
        if not mismatches or self._reconciliation_fail_closed:
            return
        self._reconciliation_fail_closed = True
        reason = (
            "reconciliation mismatch — entries halted; reduce-only exits remain allowed"
        )
        self.gateway.kill_switch.activate(reason)
        self.journal.append("reconciliation_fail_closed", {
            "reason": reason,
            "mismatches": list(mismatches),
        })

    # Feature columns worth surfacing per evaluation, when the strategy
    # computes them. Signal proximity ("how close is this lane to firing")
    # is unreadable from a binary fired/not-fired record; these make it visible.
    _EVAL_FEATURES = (
        "funding_pct",
        "close_z",
        "funding_rate",
        "er",
        "atr_pct",
        "long_score",
        "short_score",
        "tqi_long",
        "tqi_short",
        "quality_strength",
        "bull_power",
        "bear_power",
        "bbp",
        "bbp_delta",
        "bbp_hist_z",
        "bbp_hist_slope",
        "stealth_trail",
        "stealth_trend",
        "stealth_distance_atr",
        "rsi",
        "volume_z",
        "mom_persist_long",
        "mom_persist_short",
        "structure_long",
        "structure_short",
        "body_atr",
        "body_percentile",
        "expected_net_edge_bps_long",
        "expected_net_edge_bps_short",
    )
    _EVAL_THRESHOLDS = (
        "extreme_pct",
        "z_entry",
        "breakout_bars",
        "min_score",
        "min_score_delta",
        "min_tqi",
        "min_quality_strength",
        "min_momentum_persistence",
        "min_bbp_atr",
        "min_bbp_z",
        "min_bbp_slope",
        "stealth_trail_atr_mult",
        "min_volume_z",
        "min_body_atr",
        "min_body_percentile",
        "min_expected_net_edge_bps",
        "max_funding_against",
        "min_atr_pct",
        "max_atr_pct",
    )

    def _record_eval(
        self, df: pd.DataFrame, index: int, sig: SignalIntent | None,
        *, backfill: bool = False, skip_reason: str | None = None,
    ) -> None:
        """Journal one strategy evaluation — fired or not — with the feature
        values that drove it. This is the observability record that turns
        'no signal for days' from a mystery into a measurement (how far from
        each threshold every bar actually was)."""
        bar_ts = df["timestamp"].iloc[index].isoformat()
        eval_key = (
            self.strategy.strategy_id,
            self.config.symbol,
            self.config.timeframe,
            bar_ts,
        )
        if backfill and eval_key in self._backfill_eval_keys:
            return
        row = df.iloc[index]
        if sig is None and not skip_reason:
            evaluation = getattr(self.strategy, "last_evaluation", None)
            if evaluation is not None and not bool(getattr(evaluation, "accepted", False)):
                skip_reason = str(getattr(evaluation, "reason", "no signal"))
                self.last_reject_reason = skip_reason
        elif sig is None and skip_reason and not backfill:
            self.last_reject_reason = skip_reason
        features = {}
        for col in self._EVAL_FEATURES:
            if col in df.columns:
                val = float(row[col])
                features[col] = None if math.isnan(val) else round(val, 6)
        thresholds = _extract_strategy_thresholds(self.strategy, self._EVAL_THRESHOLDS)
        record = {
            "bar_ts": bar_ts,
            "strategy_id": self.strategy.strategy_id,
            "exchange": getattr(self.feed, "exchange_id", ""),
            "symbol": self.config.symbol,
            "timeframe": self.config.timeframe,
            "decision_price": (
                None
                if "close" not in df.columns or not math.isfinite(float(row["close"]))
                else round(float(row["close"]), 10)
            ),
            "mode": self.config.mode.value,
            "fired": sig is not None,
            "signal_reason": sig.reason if sig is not None else None,
            "skip_reason": skip_reason,
            "signal": _signal_payload(sig),
            "features": features,
            "thresholds": thresholds,
            "backfill": backfill,
        }
        self.evals += 1
        if backfill:
            self.backfill_evals += 1
            if sig is not None:
                self.backfill_signals += 1
        else:
            self.live_evals += 1
            if sig is not None:
                self.live_signals += 1
                _ts = df["timestamp"].iloc[index]
                self.last_fired_ts = _ts.isoformat() if hasattr(_ts, "isoformat") else str(_ts)
        self.journal.append("lane_eval", record)
        if backfill:
            self._backfill_eval_keys.add(eval_key)
        if not backfill:
            self.last_eval = record
        if sig is not None and not backfill:
            from datetime import datetime as _dt
            self._log_trade_event(
                "signal_fired", f"{sig.side} — {sig.reason}"[:140], _dt.now(UTC),
            )
        if sig is not None:
            logger.info(
                "lane eval [%s %s]%s: FIRED %s — %s",
                self.strategy.strategy_id, self.config.symbol,
                " (backfill)" if backfill else "", sig.side, sig.reason,
            )
        elif skip_reason and not backfill:
            from datetime import datetime as _dt
            self._log_trade_event(
                "entry_skipped", skip_reason[:140], _dt.now(UTC),
            )

    def _log_shadow_outcomes(
        self, outcomes: list[VirtualOutcome], now: datetime
    ) -> None:
        for outcome in outcomes:
            self._log_trade_event(
                "shadow_outcome",
                f"virtual {outcome.resolution} {outcome.side} "
                f"{outcome.virtual_net_usd:+.2f} USD after {outcome.bars_held} bars"[:140],
                now,
            )

    def _ledger_new_fills(self, now: datetime) -> None:
        """Append any fills not yet chained. The ledger is resume-aware, so a
        restart continues the chain rather than re-recording old fills."""
        if self.fill_ledger is None:
            return
        fills = self.exchange.get_fills()
        for fill in fills[self._ledgered_fills:]:
            managed_order = self.om.orders.get(fill.client_order_id)
            fee_leg = (
                "close"
                if managed_order is not None and managed_order.intent.reduce_only
                else "open"
            )
            self._log_trade_event(
                "fill",
                f"{'buy' if fill.buy else 'sell'} {fill.quantity:g} @ {fill.price:g} "
                f"fee ${fill.fee_usd:.2f} pnl ${fill.realized_pnl_usd:+.2f}"[:140],
                now,
            )
            self.fill_ledger.append({
                "ts": now.isoformat(),
                "mode": self.config.mode.value,
                "venue": getattr(self.feed, "exchange_id", "paper"),
                "strategy_id": self.strategy.strategy_id,
                "symbol": fill.symbol,
                "side": "buy" if fill.buy else "sell",
                "quantity": fill.quantity,
                "price": fill.price,
                "fee_usd": fill.fee_usd,
                "realized_pnl_usd": fill.realized_pnl_usd,
                "client_order_id": fill.client_order_id,
                "exchange_seq": fill.seq,
                "mid_at_send": fill.mid_at_send,
                "fill_price": fill.price,
                "realized_exec_bps": fill.realized_exec_bps,
                "liquidity": fill.liquidity,
                "schedule_fee_bps": (
                    self.exchange.fill_model.maker_fee_bps
                    if fill.liquidity == "maker"
                    else self.exchange.fill_model.taker_fee_bps
                ),
                "fee_leg": fee_leg,
                "hold_seconds": None,
                "close_fee_waived": fee_leg == "close" and fill.fee_usd == 0,
                "execution_label_resolved": fill.realized_exec_bps is not None,
                "execution_label_schema_version": "execution_cost_label_v1",
            })
        self._ledgered_fills = len(fills)

    def _publish_snapshot(self) -> None:
        if self.provider is None and self.alert_engine is None:
            return
        snapshot = build_snapshot(
            mode=self._mode_label(), live_trading_enabled=False,
            tracker=self.tracker, exchange=self.exchange,
            kill_switch=self.gateway.kill_switch, journal=self.journal,
            order_manager=self.om,
            feed_health=FeedHealth(
                exchange=(
                    f"{getattr(self.feed, 'exchange_id', 'feed')} "
                    f"({getattr(self.feed, 'feed_mode', 'live feed')})"
                ),
                candles="ok" if self.feed.staleness_seconds() < 120 else "stale",
                last_update_ms=self.feed.staleness_seconds() * 1000.0,
            ),
            symbol=self.config.symbol,
            strategy_id=self.strategy.strategy_id,
            recent_alerts=list(self.alert_engine.recent)
            if self.alert_engine is not None else [],
            quote=self.feed.quote,
            funding_rate=getattr(self.feed, "funding_rate", 0.0),
            session_stats={
                "started_at": self._started_at.isoformat(),
                "bars_processed": self.bars_processed,
                "evals": self.evals,
                "live_evals": self.live_evals,
                "backfill_evals": self.backfill_evals,
                "signals": self.signals,
                "live_signals": self.live_signals,
                "backfill_signals": self.backfill_signals,
                "orders_submitted": self.orders_submitted,
                "risk_rejects": self.risk_rejects,
                "sizing_skips": self.sizing_skips,
                "tick_stop_exits": self.tick_stop_exits,
                "shadow_approved": self.shadow_approved,
                "shadow_rejected": self.shadow_rejected,
                "recon_mismatches": self.recon_mismatches,
                "dropped_candles": self.dropped_candles,
                # feed-continuity guard: reduce-only reason (or None) + counters
                "degraded": self._degraded_reason,
                "gapped_candles": self.gapped_candles,
                "gap_fills": self.gap_fills,
                "discontinuity_events": self.discontinuity_events,
                "future_candles": self.future_candles,
                "timeframe": self.config.timeframe,
                # pipeline latency: bar_close_processing_ms (close -> dequeue)
                # + decision_lag_ms (bar -> signal), each {last,p50,p95,max,n}.
                # feed_lag_ms remains a compatibility alias for the former.
                "latency": self.latency.snapshot(),
                # Bounded self-recovery proof. Historical p95 remains visible;
                # only fresh, distinct samples can release a stale HARD tail.
                "latency_recovery": self._latency_recovery_snapshot(),
                # multi-TF forming+closed awareness (read-only; null if TF unsupported)
                "time_machine": self._tm_snapshot(),
                # candle-path arm-gate: cumulative skips (metric) + the CURRENT
                # block reason (None if not blocking now) so the chips reflect
                # live state, not "ever blocked this session".
                "decision_skips": dict(self._decision_skips),
                "arm_blocked": self._candle_path_arm_block(datetime.now(UTC)),
                # D-lite overlays (OBSERVE-ONLY): lane cost world + what regime_v0
                # and the cost-aware plan contract WOULD say (never gates the lane)
                "cost_profile": self.cost_profile,
                "regime": self._overlay_regime,
                "regime_would_block": self._regime_would_block,
                "plan_overlay": self._overlay_plan,
                "plan_gate_rejects": self._plan_gate_rejects,
                "trail_atr_faults": self._trail_atr_faults,
                # per-lane drawdown vs its trial limit + live trial scorecard, so a
                # lane breaching its own gate can't hide inside the fleet aggregate
                "peak_equity": self.tracker.peak_equity_usd,
                "drawdown_pct": round(self._drawdown_pct(), 2),
                "dd_limit_pct": (self.trial_meta or {}).get("max_dd_pct"),
                "trial_scorecard": self._trial_scorecard(),
                "last_fired_ts": self.last_fired_ts,
                "last_eval": self.last_eval,
                "last_reject_reason": self.last_reject_reason,
                "shadow_perf": (
                    self.scanner_observer.stats()
                    if self.scanner_observer is not None
                    else self.shadow_outcomes.stats()
                    if self.shadow_outcomes is not None
                    else None
                ),
                # Read-only operating contract for the cockpit.  This exposes
                # the purse/margin/leverage truth operators configured without
                # creating a second sizing implementation in the UI.
                "sizing_profile": {
                    "starting_equity_usd": self.config.starting_equity_usd,
                    "fixed_margin_usd": self.config.risk.fixed_margin_usd,
                    "max_leverage": self.config.risk.max_leverage_per_position,
                    "max_effective_account_leverage": (
                        self.config.risk.max_effective_account_leverage
                    ),
                    "max_symbol_exposure_usd": (
                        self.config.risk.max_exposure_per_symbol_usd
                    ),
                    "max_total_exposure_usd": (
                        self.config.risk.max_total_exposure_usd
                    ),
                    "max_open_positions": self.config.risk.max_open_positions,
                    "daily_loss_halt_enabled": (
                        self.config.risk.daily_loss_halt_enabled
                    ),
                    "profile": (
                        "fixed_margin_shadow"
                        if self.config.risk.fixed_margin_usd is not None
                        else "risk_based"
                    ),
                },
                "active_plan": (
                    {
                        "side": self._plan.signal.side,
                        "entry_bar_ts": self._plan.entry_bar_ts.isoformat(),
                        "entry_price": self._plan.exit_state.entry_price,
                        "stop_price": self._plan.exit_state.current_stop,
                        "initial_stop_price": (
                            self._plan.exit_state.initial_stop_price
                        ),
                        "take_profit_price": (
                            self._plan.exit_state.take_profit_price
                        ),
                        "take_profit_levels": list(
                            self._plan.exit_state.take_profit_levels
                        ),
                        "mfe_price": self._plan.exit_state.mfe_price,
                        "breakeven_armed": (
                            self._plan.exit_state.breakeven_armed
                        ),
                    }
                    if self._plan is not None
                    else None
                ),
                "trade_log": list(self.trade_log),
                "fill_ledger": {
                    "records": self.fill_ledger.records,
                    "chained": True,
                } if self.fill_ledger is not None else None,
                "book_metrics": getattr(self.feed, "book_metrics", None),
            },
            trial=self.trial_meta,
        )
        if self.provider is not None:
            self.provider.publish(snapshot)
        if self.alert_engine is not None:
            self.alert_engine.evaluate(snapshot)

    # --- Main loop -----------------------------------------------------------------
    # Seeded bars re-evaluated at shadow startup. 24 gives a full day of
    # observability records after any restart, at negligible cost.
    _SHADOW_PRIME_BACKFILL_BARS = 24

    # Idle tick cadence: while no closed candle is pending, the queue wait
    # times out at this interval and the loop gets a tick — tick-stop check +
    # snapshot publish. Tests shrink it via instance override.
    _IDLE_TICK_SECONDS = 5.0

    async def _prepare_strategy_for_bar(self) -> pd.DataFrame:
        """Prepare one closed-bar feature frame without blocking peer lanes.

        Multi-lane shadow runs share a single asyncio event loop.  Strategy
        preparation is CPU-bound pandas work, and running it inline lets the
        first lane at a common close (especially a 15-minute boundary) delay
        every lane queued behind it.  That queue delay is then correctly
        observed as closed-bar lag, but it is self-inflicted rather than a
        stale exchange candle.

        Offload the synchronous strategy contract to the default worker pool.
        A session cannot append another candle while awaiting this call, and
        each lane owns its own candle frame and strategy instance, so the
        objects passed to the worker remain session-local.
        """
        return await asyncio.to_thread(self.strategy.prepare, self.candles)

    def _shadow_prime(self) -> None:
        """SHADOW lanes only: backfill observability from seeded bars.

        The live loop otherwise acts only on bars that close AFTER startup, so
        the seeded interval still needs an audit trail.  Seeded bars are never
        allowed to create a new intent, including the newest one: doing so
        turns a process restart into a market decision and prices an old signal
        at the current quote.  Existing unresolved intents are restored from
        the journal by ``ShadowOutcomeTracker`` before this method runs.
        """
        if self.config.mode is not RunnerMode.SHADOW:
            return
        if len(self.candles) <= self.strategy.warmup_bars:
            return
        df = self.strategy.prepare(self.candles)
        last = len(df) - 1
        first = max(self.strategy.warmup_bars, last - self._SHADOW_PRIME_BACKFILL_BARS + 1)
        backfill_fired = 0
        for i in range(first, last):
            sig_i = self.strategy.signal(df, i)
            self._record_eval(df, i, sig_i, backfill=True)
            backfill_fired += sig_i is not None
        sig = self.strategy.signal(df, last)
        self._record_eval(
            df,
            last,
            sig,
            backfill=True,
            skip_reason=(
                "shadow_prime: historical signal observed; no restart intent"
                if sig is not None
                else None
            ),
        )
        self._record_overlays(df, last, sig)   # populate regime/plan on startup, not next close
        logger.info(
            "shadow prime [%s %s]: %d seeded bars backfilled (%d would have "
            "fired), latest -> %s",
            self.strategy.strategy_id, self.config.symbol,
            max(0, last - first), backfill_fired,
            f"{sig.side} historical signal (not submitted)" if sig is not None else "no signal",
        )

    def _is_paper_observation_lane(self) -> bool:
        trial_id = str((self.trial_meta or {}).get("trial_id") or "")
        return (
            self.config.mode is RunnerMode.PAPER
            and trial_id.endswith("_paper_observation")
        )

    async def _paper_observation_prime(self) -> None:
        """PAPER observation lanes: prime observability, never restart-enter.

        Governed paper trials deliberately do not prime on startup because a
        restart must not duplicate a live-paper entry. Observation mirrors are
        different: their purpose is operator visibility across many lanes, and
        stale scanner rows after every deploy hide the real state. So they
        journal seeded-bar evaluations exactly like shadow prime, but submit
        no orders or intents. Any fired latest bar is marked as a signal in
        lane_eval telemetry only; the paper order path still waits for a
        forward live candle.
        """
        if not self._is_paper_observation_lane():
            return
        if len(self.candles) <= self.strategy.warmup_bars:
            return
        df = self.strategy.prepare(self.candles)
        last = len(df) - 1
        first = max(self.strategy.warmup_bars, last - self._SHADOW_PRIME_BACKFILL_BARS + 1)
        backfill_fired = 0
        for i in range(first, last):
            sig_i = self.strategy.signal(df, i)
            self._record_eval(df, i, sig_i, backfill=True)
            backfill_fired += sig_i is not None
        sig = self.strategy.signal(df, last)
        self._record_eval(
            df,
            last,
            sig,
            skip_reason=(
                "paper_observation_prime: no restart order submitted"
                if sig is not None else None
            ),
        )
        logger.info(
            "paper observation prime [%s %s]: %d seeded bars backfilled "
            "(%d would have fired), latest -> %s (no restart order)",
            self.strategy.strategy_id,
            self.config.symbol,
            max(0, last - first),
            backfill_fired,
            f"{sig.side} signal" if sig is not None else "no signal",
        )

    async def run(self, *, max_bars: int | None = None,
                  deadline_seconds: float | None = None) -> RunReport:
        started = datetime.now(UTC)
        bars = 0
        prepared_warmup = self.strategy.warmup_bars

        if self.shadow_outcomes is not None and self.shadow_outcomes.has_pending:
            # restart: intents journaled before the shutdown resolve against
            # the seeded history first, so an already-hit stop or target is
            # never mis-resolved later at live prices
            self._shadow_exit_df = self.strategy.prepare(self.candles).reset_index(drop=True)
            self._log_shadow_outcomes(
                self.shadow_outcomes.replay(self._shadow_exit_df), started
            )
        if self.scanner_observer is not None:
            self.scanner_observer.restore(
                self.strategy.prepare(self.candles).reset_index(drop=True)
            )

        self._shadow_prime()
        await self._paper_observation_prime()
        self._record_runner_heartbeat("runner_started", started, force=True)

        while True:
            if max_bars is not None and bars >= max_bars:
                break
            if deadline_seconds is not None:
                elapsed = (datetime.now(UTC) - started).total_seconds()
                if elapsed >= deadline_seconds:
                    break
            raw = None
            quote_update = None
            quote_queue = getattr(self.feed, "quote_updates", None)
            acceptance_observer = (
                self.scanner_observer
                if isinstance(
                    self.scanner_observer, SqueezeAcceptanceObserveRunner
                )
                else None
            )
            if (
                acceptance_observer is not None
                and quote_queue is not None
            ):
                candle_task = asyncio.create_task(self.feed.closed_candles.get())
                quote_task = asyncio.create_task(quote_queue.get())
                done, pending = await asyncio.wait(
                    {candle_task, quote_task},
                    timeout=self._IDLE_TICK_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if candle_task in done:
                    raw = candle_task.result()
                elif quote_task in done:
                    quote_update = quote_task.result()
            else:
                try:
                    raw = await asyncio.wait_for(
                        self.feed.closed_candles.get(), timeout=self._IDLE_TICK_SECONDS
                    )
                except TimeoutError:
                    pass

            if quote_update is not None:
                self._sync_quote()
                if acceptance_observer is None:  # defensive type/runtime guard
                    continue
                # Keep the decision-timeframe clock alive before the observer
                # can ask the fail-closed arm gate for approval.  Previously
                # the immediate ``continue`` below starved Time Machine for as
                # long as quotes kept arriving, blocking almost every scanner
                # fire with ``tm_age_hard`` despite a sub-millisecond BBO.
                quote_clock = quote_update.received_ts or quote_update.ts
                self._feed_time_machine(
                    quote_clock,
                    live_mid=(quote_update.bid + quote_update.ask) / 2.0,
                )
                before_candidates = acceptance_observer.candidates
                before_approved = acceptance_observer.fires
                before_rejected = acceptance_observer.rejected
                acceptance_observer.on_quote(
                    bid=quote_update.bid,
                    ask=quote_update.ask,
                    ts=quote_update.ts,
                    received_ts=quote_update.received_ts,
                    sequence=quote_update.sequence,
                    source=quote_update.source,
                    exchange_timestamped=quote_update.exchange_timestamped,
                )
                self.signals += acceptance_observer.candidates - before_candidates
                self.shadow_approved += acceptance_observer.fires - before_approved
                self.shadow_rejected += acceptance_observer.rejected - before_rejected
                self._record_runner_heartbeat("quote_acceptance", quote_update.ts)
                self._publish_snapshot()
                continue

            if raw is None:
                # capital protection between bars: stops (and ONLY stops) are
                # evaluated against the current quote on every idle tick
                idle_now = datetime.now(UTC)
                await self._check_tick_stop(idle_now)
                if self.feed.quote is not None:
                    self._sync_quote()
                    await self._enforce_daily_factory_flatten(idle_now, idle_now)
                # stall guard: no closed bar in > _STALL_BARS × timeframe means
                # the feed/loop is wedged — go reduce-only (recoverable: a clean
                # bar resuming clears it) so we never sit silently late.
                if self._tf_seconds is not None and self._last_bar_wall is not None:
                    idle_s = (idle_now - self._last_bar_wall).total_seconds()
                    if idle_s > self._STALL_BARS * self._tf_seconds:
                        self._enter_degraded(
                            f"feed_stall:{int(idle_s)}s_no_bar", recoverable=True
                        )
                self._feed_time_machine(idle_now)  # forming-bar progress (read-only)
                self._record_runner_heartbeat("waiting_for_closed_candle", idle_now)
                self._publish_snapshot()  # keep the dashboard honest while idle
                continue

            now = datetime.now(UTC)
            # detect + heal (or fail-closed on) a feed gap BEFORE the bar reaches
            # the strategy, so contiguous-index indicators never span a hole.
            # Only the LIVE stream's internal continuity is guarded here — the
            # warmup→first-live handover is build_lane's contract (it REST-seeds
            # up to now), so the first live bar just establishes the baseline.
            if self._last_bar_wall is not None and not await self._guard_candle_continuity(
                raw, now
            ):
                continue
            if not self._append_candle(raw) or not self._sync_quote():
                continue
            self._last_bar_wall = now
            self._feed_time_machine(now, closed_row=raw)  # read-only, fail-closed
            bars += 1
            self.bars_processed += 1
            self.tracker.on_bar(now)
            self._maybe_daily_report(now)

            bar = self.candles.iloc[-1]
            bar_clock = pd.Timestamp(bar["timestamp"]).to_pydatetime()
            # closed-bar processing lag: how stale this candle is now that the
            # bar path is allowed to act on it.
            # bar_clock is the candle OPEN; it closed one timeframe later, so
            # (now - close) captures network + exchange emit + any loop-
            # saturation queue-wait. Clock skew can make it slightly negative
            # (data-quality gate bounds skew separately) — floor at zero.
            if self._tf_seconds is not None:
                close_dt = bar_clock + timedelta(seconds=self._tf_seconds)
                self.latency.record_bar_close(close_dt, now)
            await self._manage_exit(bar, now)
            await self._enforce_daily_factory_flatten(bar_clock, now)
            await self._manage_pending_entry(now)
            await self._cancel_pending_entry_for_daily_factory(bar_clock, now)
            self._guard_orphaned_position()

            # A strategy frame is expensive to construct and both the scanner
            # lifecycle and shadow-outcome resolver need the identical view of
            # this closed bar.  Cache it for this iteration so one lane never
            # performs the same pandas feature build twice.
            prepared_frame: pd.DataFrame | None = None

            # Canonical scanner lanes own their complete arm -> trigger ->
            # exit -> cost lifecycle. They still pass sizing and the central
            # gateway through ``_approve_scanner_fire``, but never create an
            # OrderIntent submission. Running the observer here (once per
            # forward closed bar) keeps replay and VM shadow semantics aligned.
            if (
                self.scanner_observer is not None
                and len(self.candles) > prepared_warmup
            ):
                _dec_t0 = time.perf_counter()
                scanner_df = await self._prepare_strategy_for_bar()
                prepared_frame = scanner_df
                scanner_idx = len(scanner_df) - 1
                before_candidates = self.scanner_observer.candidates
                before_approved = self.scanner_observer.fires
                before_rejected = self.scanner_observer.rejected
                fire = self.scanner_observer.on_prepared_bar(
                    scanner_df, scanner_idx, bar_clock
                )
                self.signals += self.scanner_observer.candidates - before_candidates
                self.shadow_approved += self.scanner_observer.fires - before_approved
                self.shadow_rejected += self.scanner_observer.rejected - before_rejected
                approval = self.scanner_observer.last_approval
                scanner_sig = (
                    SignalIntent(
                        side=fire.side,
                        stop_price=fire.stop,
                        take_profit_price=None,
                        reason=fire.reason,
                    )
                    if fire is not None and approval is not None and approval.approved
                    else None
                )
                self._record_eval(
                    scanner_df,
                    scanner_idx,
                    scanner_sig,
                    skip_reason=(
                        approval.explanation
                        if fire is not None and approval is not None and not approval.approved
                        else None
                    ),
                )
                self._record_overlays(scanner_df, scanner_idx, scanner_sig)
                self.latency.record(
                    DECISION_LAG_MS, (time.perf_counter() - _dec_t0) * 1000.0
                )

            shadow_book_reserved = (
                self.config.mode is RunnerMode.SHADOW
                and self.shadow_outcomes is not None
                and self.shadow_outcomes.has_pending
            )
            if self._plan is None and self._pending_entry is None \
                    and self.scanner_observer is None \
                    and not shadow_book_reserved \
                    and self._degraded_reason is None \
                    and len(self.candles) > prepared_warmup:
                # decision lag: candle in hand -> signal decided (prepare +
                # signal). perf_counter is monotonic — immune to wall-clock
                # steps. Measured on every eval, blocked or not, so a slow
                # strategy shows up even when it never fires.
                _dec_t0 = time.perf_counter()
                df = await self._prepare_strategy_for_bar()
                prepared_frame = df
                idx = len(df) - 1
                factory_block = self._daily_factory_entry_block_reason(bar_clock)
                allowed, block_reason = self.protections.entries_allowed(idx)
                cp_block = self._candle_path_arm_block(now)
                if cp_block is not None:
                    # decision-TF candle path unsafe to arm on: block the NEW
                    # entry (exits already ran above). Fail-closed for entries.
                    sig = None
                    self._decision_skips[cp_block] = self._decision_skips.get(cp_block, 0) + 1
                    self._record_eval(df, idx, sig, skip_reason=f"candle_path:{cp_block}")
                    self._log_trade_event(
                        "candle_path_blocked", f"new arm blocked: decision-TF {cp_block}"[:140], now
                    )
                elif factory_block is not None:
                    sig = None
                    self._record_eval(df, idx, sig, skip_reason=factory_block)
                    self._log_trade_event(
                        "daily_factory_blocked", factory_block[:140], now
                    )
                elif not allowed:
                    sig = None
                    self._record_eval(df, idx, sig, skip_reason=block_reason)
                    if not self._protection_block_logged:
                        self._log_trade_event(
                            "protection_blocked", block_reason[:140], now
                        )
                        self._protection_block_logged = True
                else:
                    self._protection_block_logged = False
                    sig = self.strategy.signal(df, idx)
                    self._record_eval(df, idx, sig)
                self.latency.record(
                    DECISION_LAG_MS, (time.perf_counter() - _dec_t0) * 1000.0
                )
                self._record_overlays(df, idx, sig)   # OBSERVE-ONLY: regime + plan
                if sig is not None:
                    self.signals += 1
                    await self._submit_entry(sig, now)

            if self.shadow_outcomes is not None:
                # resolve earlier shadow intents against this closed bar; the
                # intent journaled above (bar_ts == this bar) is untouched —
                # its virtual fill is the NEXT bar, like the backtester
                if prepared_frame is None:
                    prepared_frame = await self._prepare_strategy_for_bar()
                self._shadow_exit_df = prepared_frame.reset_index(drop=True)
                self._log_shadow_outcomes(
                    # feed the IDENTICAL canonical ATR the paper/live trail uses,
                    # so the shadow stop ratchets to the same value on the same bar
                    self.shadow_outcomes.resolve_bar(
                        self._shadow_exit_df.iloc[-1], atr=self._trail_atr()
                    ),
                    now,
                )

            self._bars_since_reconcile += 1
            if self._bars_since_reconcile >= self.config.reconcile_every_bars \
                    or self.om.has_unresolved_orders:
                report = self._reconcile()
                self.recon_mismatches += len(report.mismatches)
                self._bars_since_reconcile = 0

            self._ledger_new_fills(now)
            self._record_runner_heartbeat("bar_processed", now, force=True)

            if self.account_store is not None:
                self.account_store.save_from(
                    self.exchange, self.tracker, plan=self._serialize_plan()
                )
            if self.funnel_store is not None:
                self.funnel_store.save_from(self)
            if self.latency_store is not None:
                self.latency_store.save_from(self.latency)
            if self.equity_history_path is not None:
                try:
                    await asyncio.to_thread(
                        _append_equity_history,
                        self.equity_history_path,
                        now,
                        self.tracker.equity_usd(),
                    )
                except OSError as exc:
                    logger.warning("equity history write failed: %s", exc)
            self._publish_snapshot()

        final = self._reconcile()
        self.recon_mismatches += len(final.mismatches)
        fills = self.exchange.get_fills()
        report = RunReport(
            mode=f"{self.config.mode.value}_live", symbol=self.config.symbol,
            strategy_id=self.strategy.strategy_id,
            bars_processed=bars, signals_generated=self.signals,
            orders_submitted=self.orders_submitted, fills=len(fills),
            fees_usd=sum(f.fee_usd for f in fills),
            realized_pnl_usd=self.exchange.get_balances()["USDT"]
            - self.config.starting_equity_usd,
            unrealized_pnl_usd=self.tracker.unrealized_pnl_usd(),
            max_drawdown_pct=0.0,  # session-level dd needs longer runs; journal has equity
            risk_rejects=self.risk_rejects, sizing_skips=self.sizing_skips,
            shadow_approved=self.shadow_approved,
            shadow_rejected=self.shadow_rejected,
            reconciliation_mismatches=self.recon_mismatches,
            final_equity_usd=self.tracker.equity_usd(),
        )
        self.journal.append("live_paper_report", report.to_dict())
        return report

    def _reconcile(self):
        report = self.reconciler.run()
        self._fail_closed_on_reconciliation(report.mismatches)
        for coid in report.resolved_orders:
            plan = self._parked_entries.pop(coid, None)
            if plan is None:
                self._resolve_pending_exit(coid)
                continue
            order = self.om.orders[coid]
            if order.state in _EXIT_ACCEPTED_STATES and self._plan is None:
                self._seed_plan_from_venue(plan, coid)
                self._plan = plan
        return report

    def _resolve_pending_exit(self, client_order_id: str) -> None:
        base_key = next(
            (
                key
                for key, pending_id in self._pending_exit_orders.items()
                if pending_id == client_order_id
            ),
            None,
        )
        if base_key is None:
            return
        order = self.om.orders[client_order_id]
        reason = _exit_reason_from_key(base_key)
        if order.state in _EXIT_ACCEPTED_STATES:
            final = self._pending_exit_finals.pop(base_key, True)
            decision = self._pending_exit_decisions.pop(base_key, None)
            if decision is not None and not decision.final and self._plan is not None:
                self._plan.exit_state.mark_accepted(decision)
            self.journal.append("exit_plan_cleared_after_reconciliation", {
                "intent_key": base_key,
                "client_order_id": client_order_id,
                "reason": reason,
                "state": order.state.value,
                "final": final,
            })
            self._pending_exit_orders.pop(base_key, None)
            self._mark_exit_accepted(reason, final=final)
            return
        if order.state in _EXIT_RETRYABLE_STATES:
            self._pending_exit_orders.pop(base_key, None)
            self._pending_exit_finals.pop(base_key, None)
            self._pending_exit_decisions.pop(base_key, None)
            self._exit_retry_attempts[base_key] = self._exit_retry_attempts.get(base_key, 0) + 1
            self.journal.append("exit_plan_preserved_after_reconciliation", {
                "intent_key": base_key,
                "client_order_id": client_order_id,
                "reason": reason,
                "state": order.state.value,
                "next_retry": self._exit_intent_key(base_key),
            })


def _exit_reason_from_key(intent_key: str) -> str:
    parts = intent_key.split("|")
    return parts[2] if len(parts) > 2 else "exit"
