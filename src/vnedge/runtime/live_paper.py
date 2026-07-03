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
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from vnedge.dashboard.state_snapshot import FeedHealth, build_snapshot
from vnedge.execution.idempotency import make_intent_key
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState
from vnedge.paper.paper_reconciliation import PaperReconciler
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.position_sizer import size_position
from vnedge.risk.risk_manager import OrderIntent, PreTradeRiskGateway
from vnedge.runtime.portfolio_tracker import PortfolioTracker
from vnedge.runtime.run_report import RunReport
from vnedge.runtime.runner_config import RunnerConfig
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

logger = logging.getLogger(__name__)


@dataclass
class _LivePlan:
    signal: SignalIntent
    entry_bar_ts: pd.Timestamp


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
    ) -> None:
        self.strategy = strategy
        self.feed = feed
        self.candles = history.reset_index(drop=True)
        self.config = config
        self.gateway = gateway
        self.om = order_manager
        self.exchange = exchange
        self.journal = journal
        self.provider = snapshot_provider
        self.tracker = PortfolioTracker(exchange, config.starting_equity_usd)
        self.reconciler = PaperReconciler(order_manager, exchange)
        self.signals = self.orders_submitted = self.risk_rejects = 0
        self.sizing_skips = self.dropped_candles = self.recon_mismatches = 0
        self._plan: _LivePlan | None = None
        self._bars_since_reconcile = 0

    # --- Internals ---------------------------------------------------------------
    def _sync_quote(self) -> bool:
        if self.feed.quote is None:
            return False
        bid, ask = self.feed.quote
        self.exchange.set_quote(self.config.symbol, bid, ask)
        return True

    def _append_candle(self, raw_row: list) -> bool:
        """Incremental quality gate: strictly-forward timestamps only."""
        ts = pd.to_datetime(raw_row[0], unit="ms", utc=True)
        if len(self.candles) and ts <= self.candles["timestamp"].iloc[-1]:
            self.dropped_candles += 1
            logger.warning("dropped non-forward candle %s", ts)
            return False
        row = {
            "timestamp": ts,
            "open": float(raw_row[1]), "high": float(raw_row[2]),
            "low": float(raw_row[3]), "close": float(raw_row[4]),
            "volume": float(raw_row[5]),
        }
        self.candles = pd.concat(
            [self.candles, pd.DataFrame([row])], ignore_index=True
        )
        return True

    async def _submit_entry(self, sig: SignalIntent, now: datetime) -> None:
        bid, ask = self.feed.quote
        ref_price = ask if sig.side == "long" else bid
        sizing = size_position(
            equity_usd=self.tracker.equity_usd(), entry_price=ref_price,
            stop_price=sig.stop_price, side=sig.side,
            config=self.config.risk, limits=self.config.limits,
        )
        if not sizing.approved:
            self.sizing_skips += 1
            return
        intent = OrderIntent(
            symbol=self.config.symbol, side=sig.side, quantity=sizing.quantity,
            notional_usd=sizing.notional_usd,
            leverage=max(sizing.required_leverage, 1.0),
            reduce_only=False, strategy_id=self.strategy.strategy_id,
        )
        key = make_intent_key(
            self.strategy.strategy_id, self.config.symbol, sig.side,
            self.candles["timestamp"].iloc[-1],
        )
        order = await self.om.submit(
            intent, self.tracker.account_state(), self.feed.market_state(), key, now=now
        )
        if order.state is OrderState.RISK_REJECTED:
            self.risk_rejects += 1
        else:
            self.orders_submitted += 1
            if order.state is OrderState.ACKNOWLEDGED:
                self._plan = _LivePlan(sig, self.candles["timestamp"].iloc[-1])

    async def _manage_exit(self, bar: pd.Series, now: datetime) -> None:
        if self._plan is None:
            return
        sig = self._plan.signal
        high, low = float(bar["high"]), float(bar["low"])
        reason = None
        if sig.side == "long":
            if low <= sig.stop_price:
                reason = "stop"
            elif sig.take_profit_price and high >= sig.take_profit_price:
                reason = "take_profit"
        else:
            if high >= sig.stop_price:
                reason = "stop"
            elif sig.take_profit_price and low <= sig.take_profit_price:
                reason = "take_profit"
        if reason is None:
            return
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        if pos is None:
            self._plan = None
            return
        intent = OrderIntent(
            symbol=self.config.symbol,
            side="short" if pos.quantity > 0 else "long",
            quantity=abs(pos.quantity), notional_usd=0.0, leverage=1.0,
            reduce_only=True, strategy_id=self.strategy.strategy_id,
        )
        order = await self.om.submit(
            intent, self.tracker.account_state(), self.feed.market_state(),
            intent_key=f"exit|{self.config.symbol}|{reason}|{int(bar['timestamp'].value)}",
            now=now,
        )
        self.orders_submitted += 1
        self.journal.append("live_paper_exit", {"reason": reason, "state": order.state.value})
        self._plan = None

    def _publish_snapshot(self) -> None:
        if self.provider is None:
            return
        self.provider.publish(
            build_snapshot(
                mode="paper (live data)", live_trading_enabled=False,
                tracker=self.tracker, exchange=self.exchange,
                kill_switch=self.gateway.kill_switch, journal=self.journal,
                order_manager=self.om,
                feed_health=FeedHealth(
                    exchange=f"{getattr(self.feed, 'exchange_id', 'feed')} (live ws)",
                    candles="ok" if self.feed.staleness_seconds() < 120 else "stale",
                    last_update_ms=self.feed.staleness_seconds() * 1000.0,
                ),
            )
        )

    # --- Main loop -----------------------------------------------------------------
    async def run(self, *, max_bars: int | None = None,
                  deadline_seconds: float | None = None) -> RunReport:
        started = datetime.now(UTC)
        bars = 0
        prepared_warmup = self.strategy.warmup_bars

        while True:
            if max_bars is not None and bars >= max_bars:
                break
            if deadline_seconds is not None:
                elapsed = (datetime.now(UTC) - started).total_seconds()
                if elapsed >= deadline_seconds:
                    break
            try:
                raw = await asyncio.wait_for(self.feed.closed_candles.get(), timeout=5.0)
            except asyncio.TimeoutError:
                self._publish_snapshot()  # keep the dashboard honest while idle
                continue

            now = datetime.now(UTC)
            if not self._append_candle(raw) or not self._sync_quote():
                continue
            bars += 1
            self.tracker.on_bar(now)

            bar = self.candles.iloc[-1]
            await self._manage_exit(bar, now)

            if self._plan is None and len(self.candles) > prepared_warmup:
                df = self.strategy.prepare(self.candles)
                sig = self.strategy.signal(df, len(df) - 1)
                if sig is not None:
                    self.signals += 1
                    await self._submit_entry(sig, now)

            self._bars_since_reconcile += 1
            if self._bars_since_reconcile >= self.config.reconcile_every_bars \
                    or self.om.has_unresolved_orders:
                report = self.reconciler.run()
                self.recon_mismatches += len(report.mismatches)
                self._bars_since_reconcile = 0

            self._publish_snapshot()

        final = self.reconciler.run()
        self.recon_mismatches += len(final.mismatches)
        fills = self.exchange.get_fills()
        report = RunReport(
            mode="paper_live", symbol=self.config.symbol,
            strategy_id=self.strategy.strategy_id,
            bars_processed=bars, signals_generated=self.signals,
            orders_submitted=self.orders_submitted, fills=len(fills),
            fees_usd=sum(f.fee_usd for f in fills),
            realized_pnl_usd=self.exchange.get_balances()["USDT"]
            - self.config.starting_equity_usd,
            unrealized_pnl_usd=self.tracker.unrealized_pnl_usd(),
            max_drawdown_pct=0.0,  # session-level dd needs longer runs; journal has equity
            risk_rejects=self.risk_rejects, sizing_skips=self.sizing_skips,
            shadow_approved=0, shadow_rejected=0,
            reconciliation_mismatches=self.recon_mismatches,
            final_equity_usd=self.tracker.equity_usd(),
        )
        self.journal.append("live_paper_report", report.to_dict())
        return report
