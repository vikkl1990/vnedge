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
import logging
import math
from dataclasses import asdict, dataclass
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
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.runtime.shadow_outcomes import ShadowOutcomeTracker, VirtualOutcome
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
        account_store=None,  # optional PaperAccountStore for crash/restart resume
        alert_engine=None,  # optional AlertEngine — same snapshot, guarded fanout
        equity_history_path=None,  # optional JSONL of (ts, equity) per bar
        trial_meta=None,  # optional dict shown on the dashboard governance panel
        fill_ledger=None,  # optional FillLedger — hash-chained execution record
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
        self.account_store = account_store
        self.alert_engine = alert_engine
        self.equity_history_path = equity_history_path
        self.fill_ledger = fill_ledger
        # baseline against the EXCHANGE's fill list (resets each session), not
        # the ledger's total record count (survives restarts) — else every
        # post-restart fill would be sliced away and never chained/logged
        self._ledgered_fills = len(exchange.get_fills())
        self.trial_meta = trial_meta
        self.bars_processed = 0
        self._started_at = datetime.now(UTC)
        self.tracker = PortfolioTracker(exchange, config.starting_equity_usd)
        self.reconciler = PaperReconciler(order_manager, exchange)
        self.signals = self.orders_submitted = self.risk_rejects = 0
        self.sizing_skips = self.dropped_candles = self.recon_mismatches = 0
        self.shadow_approved = self.shadow_rejected = 0
        self.tick_stop_exits = 0
        # SHADOW lanes never fill, so per-lane edge is invisible without
        # virtual resolution: approved intents are resolved forward on
        # closed bars with backtester semantics (journal = durable store).
        self.shadow_outcomes: ShadowOutcomeTracker | None = (
            ShadowOutcomeTracker(
                journal,
                fill_model=exchange.fill_model,
                max_holding_bars=config.max_holding_bars,
            )
            if config.mode is RunnerMode.SHADOW
            else None
        )
        self.last_eval: dict | None = None
        # chronological trade narrative for the dashboard journal panel:
        # fired signals, gateway verdicts, submissions, fills, exits
        from collections import deque
        self.trade_log: deque = deque(maxlen=40)
        self._plan: _LivePlan | None = None
        self._parked_entries: dict[str, _LivePlan] = {}
        self._orphan_position_guarded = False
        self._reconciliation_fail_closed = False
        self._bars_since_reconcile = 0
        self._entry_cooldown_bars = 0
        self._report_day = None
        self._day_open_equity = config.starting_equity_usd
        self._day_open_fills = 0

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

    def _log_trade_event(self, event: str, detail: str, now: datetime) -> None:
        self.trade_log.append({
            "ts": now.isoformat(), "event": event, "detail": detail,
        })

    def _mode_label(self) -> str:
        if self.config.mode is RunnerMode.SHADOW:
            return "shadow (live data)"
        return "paper (live data)"

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
            self._log_trade_event("sizing_skip", f"{sig.side} rejected by sizing: {', '.join(sizing.reasons)}"[:140], now)
            return
        intent = OrderIntent(
            symbol=self.config.symbol, side=sig.side, quantity=sizing.quantity,
            notional_usd=sizing.notional_usd,
            leverage=max(sizing.required_leverage, 1.0),
            reduce_only=False, strategy_id=self.strategy.strategy_id,
        )
        decision_bar_ts = self.candles["timestamp"].iloc[-1]
        key = make_intent_key(
            self.strategy.strategy_id, self.config.symbol, sig.side,
            decision_bar_ts,
        )
        if self.config.mode is RunnerMode.SHADOW:
            decision = self.gateway.evaluate(
                intent, self.tracker.account_state(), self.feed.market_state(), now=now
            )
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
                "bar_ts": decision_bar_ts.isoformat(),
            })
            if decision.approved:
                self.shadow_approved += 1
                if self.shadow_outcomes is not None:
                    self.shadow_outcomes.track(
                        intent_key=key, side=sig.side,
                        quantity=intent.quantity,
                        notional_usd=intent.notional_usd,
                        stop_price=sig.stop_price,
                        take_profit_price=sig.take_profit_price,
                        decision_bar_ts=decision_bar_ts,
                        signal_reason=sig.reason,
                    )
                self._log_trade_event(
                    "shadow_approved",
                    f"{sig.side} {intent.quantity:g} @ ~{ref_price:g} — {sig.reason}"[:140],
                    now,
                )
            else:
                self.shadow_rejected += 1
                self._log_trade_event(
                    "shadow_rejected",
                    f"{sig.side} — failed: {', '.join(decision.failed_checks)}"[:140],
                    now,
                )
            return
        order = await self.om.submit(
            intent, self.tracker.account_state(), self.feed.market_state(), key, now=now
        )
        if order.state is OrderState.RISK_REJECTED:
            self.risk_rejects += 1
            self._log_trade_event("risk_rejected", f"{sig.side} — gateway rejected entry"[:140], now)
        else:
            self.orders_submitted += 1
            self._log_trade_event(
                "order_submitted",
                f"{sig.side} {intent.quantity:g} ({order.state.value}) — {sig.reason}"[:140],
                now,
            )
            plan = _LivePlan(sig, self.candles["timestamp"].iloc[-1])
            if order.state is OrderState.ACKNOWLEDGED:
                self._plan = plan
            elif order.state is OrderState.TIMEOUT_UNKNOWN:
                self._parked_entries[order.client_order_id] = plan

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
        order = await self._submit_exit(reason, int(bar["timestamp"].value), now)
        if order is None:
            return
        self.journal.append("live_paper_exit", {"reason": reason, "state": order.state.value})
        self._log_trade_event("exit", f"{reason} ({order.state.value})"[:140], now)

    async def _submit_exit(self, reason: str, key_ts: int, now: datetime):
        """Shared reduce-only exit submission — the ONLY way a plan closes.

        Both the bar-close path (_manage_exit) and the tick-stop path
        (_check_tick_stop) flow through here, so every exit passes the same
        gateway/journal/OrderManager pipeline and clears the plan the same
        way. Returns the ManagedOrder, or None if no position existed (the
        plan is cleared regardless)."""
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        if pos is None:
            self._plan = None
            return None
        intent = OrderIntent(
            symbol=self.config.symbol,
            side="short" if pos.quantity > 0 else "long",
            quantity=abs(pos.quantity), notional_usd=0.0, leverage=1.0,
            reduce_only=True, strategy_id=self.strategy.strategy_id,
        )
        order = await self.om.submit(
            intent, self.tracker.account_state(), self.feed.market_state(),
            intent_key=f"exit|{self.config.symbol}|{reason}|{key_ts}",
            now=now,
        )
        self.orders_submitted += 1
        self._plan = None
        self._entry_cooldown_bars = max(
            self._entry_cooldown_bars,
            self.config.post_exit_cooldown_bars,
        )
        return order

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
        breached = bid <= sig.stop_price if sig.side == "long" else ask >= sig.stop_price
        if not breached:
            return
        self._sync_quote()  # exit must fill at the breach quote, not the last bar's
        # key_ts = entry bar: one tick-stop intent per plan, minted once —
        # never re-derived from the (wall-clock) breach time
        order = await self._submit_exit("tick_stop", int(entry_bar_ts.value), now)
        if order is None:
            return
        self.tick_stop_exits += 1
        trigger_px = bid if sig.side == "long" else ask
        self.journal.append("tick_stop_exit", {
            "reason": "tick_stop",
            "state": order.state.value,
            "side": sig.side,
            "stop_price": sig.stop_price,
            "take_profit_price": sig.take_profit_price,
            "bid": bid,
            "ask": ask,
            "entry_bar_ts": entry_bar_ts.isoformat(),
            "signal_reason": sig.reason,
        })
        self._log_trade_event(
            "exit",
            f"tick_stop {sig.side} — {'bid' if sig.side == 'long' else 'ask'} "
            f"{trigger_px:g} breached stop {sig.stop_price:g} ({order.state.value})"[:140],
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
            "reason": sig.reason,
            "entry_bar_ts": self._plan.entry_bar_ts.isoformat(),
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
        if stored is not None:
            sig = SignalIntent(
                stored["side"], stop_price=float(stored["stop_price"]),
                take_profit_price=(float(stored["take_profit_price"])
                                   if stored.get("take_profit_price") is not None else None),
                reason=stored.get("reason", "restored plan"),
            )
            self._plan = _LivePlan(sig, pd.Timestamp(stored["entry_bar_ts"]))
            self.journal.append("plan_restored", dict(stored))
            logger.info("trade plan restored from account store: %s", sig.reason)
            return
        pos = positions[0]
        if len(self.candles) <= self.strategy.warmup_bars:
            return
        df = self.strategy.prepare(self.candles)
        sig = self.strategy.synthesize_exit_plan(
            df, len(df) - 1, pos.side, pos.entry_price
        )
        if sig is None:
            return  # orphan guard will handle it (entries halted, manual flatten)
        self._plan = _LivePlan(sig, df["timestamp"].iloc[-1])
        self.journal.append("plan_rebuilt_on_resume", {
            "side": sig.side, "stop_price": sig.stop_price,
            "take_profit_price": sig.take_profit_price, "reason": sig.reason,
        })
        logger.info("trade plan REBUILT on resume: %s", sig.reason)

    def _guard_orphaned_position(self) -> None:
        if self._orphan_position_guarded or self._plan is not None or self._parked_entries:
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
    _EVAL_FEATURES = ("funding_pct", "close_z", "er", "atr_pct")
    _EVAL_THRESHOLDS = ("extreme_pct", "z_entry", "breakout_bars", "min_score")

    def _record_eval(
        self, df: pd.DataFrame, index: int, sig: SignalIntent | None,
        *, backfill: bool = False, skip_reason: str | None = None,
    ) -> None:
        """Journal one strategy evaluation — fired or not — with the feature
        values that drove it. This is the observability record that turns
        'no signal for days' from a mystery into a measurement (how far from
        each threshold every bar actually was)."""
        row = df.iloc[index]
        features = {}
        for col in self._EVAL_FEATURES:
            if col in df.columns:
                val = float(row[col])
                features[col] = None if math.isnan(val) else round(val, 6)
        thresholds = {}
        for attr in self._EVAL_THRESHOLDS:
            val = getattr(self.strategy, attr, None)
            if isinstance(val, (int, float)):
                thresholds[attr] = val
        record = {
            "bar_ts": df["timestamp"].iloc[index].isoformat(),
            "strategy_id": self.strategy.strategy_id,
            "symbol": self.config.symbol,
            "mode": self.config.mode.value,
            "fired": sig is not None,
            "signal_reason": sig.reason if sig is not None else None,
            "skip_reason": skip_reason,
            "features": features,
            "thresholds": thresholds,
            "backfill": backfill,
        }
        self.journal.append("lane_eval", record)
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
                "signals": self.signals,
                "orders_submitted": self.orders_submitted,
                "risk_rejects": self.risk_rejects,
                "sizing_skips": self.sizing_skips,
                "tick_stop_exits": self.tick_stop_exits,
                "shadow_approved": self.shadow_approved,
                "shadow_rejected": self.shadow_rejected,
                "recon_mismatches": self.recon_mismatches,
                "dropped_candles": self.dropped_candles,
                "last_eval": self.last_eval,
                "shadow_perf": self.shadow_outcomes.stats()
                if self.shadow_outcomes is not None else None,
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

    async def _shadow_prime(self) -> None:
        """SHADOW lanes only: evaluate recent already-closed (seeded) bars
        once at startup — the newest live (may submit a shadow intent), the
        rest as backfill observability records.

        The live loop otherwise acts only on bars that close AFTER startup, so
        a restart silently discards an already-armed condition while it waits
        up to a full bar for the next close — with frequent restarts a slow-bar
        strategy may never get a single decision opportunity, and the bars a
        restart skipped left no record at all (the 2026-07-04 signal cluster
        was part-missed exactly this way). Shadow lanes never fill, so
        re-evaluating bars is safe: backfilled bars journal lane_eval records
        ONLY — an intent is submitted solely for the newest bar. Deliberately
        NOT done for paper/live modes, where re-entering on restart could
        double a position.
        """
        if self.config.mode is not RunnerMode.SHADOW:
            return
        if len(self.candles) <= self.strategy.warmup_bars:
            return
        # let the live feed publish its first top-of-book so sizing has a
        # real reference price (immediate when a quote is already present)
        for _ in range(30):
            if self.feed.quote is not None:
                break
            await asyncio.sleep(0.5)
        if self.feed.quote is None:
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
        self._record_eval(df, last, sig)
        logger.info(
            "shadow prime [%s %s]: %d seeded bars backfilled (%d would have "
            "fired), latest -> %s",
            self.strategy.strategy_id, self.config.symbol,
            max(0, last - first), backfill_fired,
            f"{sig.side} intent" if sig is not None else "no signal",
        )
        if sig is not None:
            self.signals += 1
            await self._submit_entry(sig, datetime.now(UTC))

    async def run(self, *, max_bars: int | None = None,
                  deadline_seconds: float | None = None) -> RunReport:
        started = datetime.now(UTC)
        bars = 0
        prepared_warmup = self.strategy.warmup_bars

        if self.shadow_outcomes is not None and self.shadow_outcomes.has_pending:
            # restart: intents journaled before the shutdown resolve against
            # the seeded history first, so an already-hit stop or target is
            # never mis-resolved later at live prices
            self._log_shadow_outcomes(
                self.shadow_outcomes.replay(self.candles), started
            )

        await self._shadow_prime()

        while True:
            if max_bars is not None and bars >= max_bars:
                break
            if deadline_seconds is not None:
                elapsed = (datetime.now(UTC) - started).total_seconds()
                if elapsed >= deadline_seconds:
                    break
            try:
                raw = await asyncio.wait_for(
                    self.feed.closed_candles.get(), timeout=self._IDLE_TICK_SECONDS
                )
            except asyncio.TimeoutError:
                # capital protection between bars: stops (and ONLY stops) are
                # evaluated against the current quote on every idle tick
                await self._check_tick_stop(datetime.now(UTC))
                self._publish_snapshot()  # keep the dashboard honest while idle
                continue

            now = datetime.now(UTC)
            if not self._append_candle(raw) or not self._sync_quote():
                continue
            bars += 1
            self.bars_processed += 1
            self.tracker.on_bar(now)
            self._maybe_daily_report(now)

            bar = self.candles.iloc[-1]
            await self._manage_exit(bar, now)
            self._guard_orphaned_position()

            if self._plan is None and len(self.candles) > prepared_warmup:
                df = self.strategy.prepare(self.candles)
                if self._entry_cooldown_bars > 0:
                    sig = None
                    self._record_eval(
                        df,
                        len(df) - 1,
                        sig,
                        skip_reason=(
                            "post_exit_cooldown: "
                            f"{self._entry_cooldown_bars} bar(s) remaining"
                        ),
                    )
                    self._entry_cooldown_bars -= 1
                else:
                    sig = self.strategy.signal(df, len(df) - 1)
                    self._record_eval(df, len(df) - 1, sig)
                if sig is not None:
                    self.signals += 1
                    await self._submit_entry(sig, now)

            if self.shadow_outcomes is not None:
                # resolve earlier shadow intents against this closed bar; the
                # intent journaled above (bar_ts == this bar) is untouched —
                # its virtual fill is the NEXT bar, like the backtester
                self._log_shadow_outcomes(self.shadow_outcomes.resolve_bar(bar), now)

            self._bars_since_reconcile += 1
            if self._bars_since_reconcile >= self.config.reconcile_every_bars \
                    or self.om.has_unresolved_orders:
                report = self._reconcile()
                self.recon_mismatches += len(report.mismatches)
                self._bars_since_reconcile = 0

            self._ledger_new_fills(now)

            if self.account_store is not None:
                self.account_store.save_from(
                    self.exchange, self.tracker, plan=self._serialize_plan()
                )
            if self.equity_history_path is not None:
                try:
                    import json as _json

                    with open(self.equity_history_path, "a", encoding="utf-8") as f:
                        f.write(_json.dumps({
                            "ts": now.isoformat(),
                            "equity": round(self.tracker.equity_usd(), 4),
                        }) + "\n")
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
                continue
            order = self.om.orders[coid]
            if order.state in (
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.FILLED,
            ) and self._plan is None:
                self._plan = plan
        return report
