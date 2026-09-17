"""Paper/shadow runner — the loop that makes the mode ladder walkable.

    bar -> quotes -> tracker -> [pending entry fill] -> exit management
        -> signal at close -> periodic reconciliation -> report

One loop serves both modes ON PURPOSE: a separate shadow runner would be a
second execution path that could drift from the paper path — the exact thing
the design forbids. In SHADOW mode the pipeline runs up to and including the
risk verdict, journals it, and stops there: no submission, no balance change.

Execution discipline mirrors the backtester: signals are taken at bar close
and filled at the next bar's open; stops are checked against bar high/low
with stop-beats-take-profit ordering; exits are reduce-only market orders
through the same OrderManager as entries. There is no strategy-to-broker
shortcut anywhere in this file.

Unknown-order policy: an entry that lands in TIMEOUT_UNKNOWN parks its trade
plan; the OrderManager already blocks further risk-increasing submissions,
and the runner activates or discards the plan when scheduled reconciliation
resolves the order from venue truth.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import pandas as pd

from vnedge.execution.evidence import CostDecisionEvidence, ExecutionEvidence
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import ManagedOrder, OrderState
from vnedge.paper.paper_reconciliation import PaperReconciler
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.cost_gate import CostGate
from vnedge.risk.position_sizer import size_position
from vnedge.risk.risk_manager import OrderIntent, PreTradeRiskGateway
from vnedge.runtime.active_exit import (
    ActiveExitDecision,
    ActiveExitState,
    ExitEngine,
    ExitEngineConfig,
)
from vnedge.runtime.execution_contract import AdapterKind, ExecutionContext
from vnedge.runtime.execution_kernel import build_kernel
from vnedge.runtime.market_replay import MarketReplay, quote_from_price
from vnedge.runtime.portfolio_tracker import PortfolioTracker
from vnedge.runtime.run_report import RunReport
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent, bind_signal_decision
from vnedge.strategy.indicators import atr as _atr_indicator

logger = logging.getLogger(__name__)

_EXIT_ACCEPTED_STATES = frozenset(
    {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}
)


@dataclass
class _TradePlan:
    signal: SignalIntent
    order: ManagedOrder
    entry_bar: int
    exit_state: ActiveExitState


class PaperRunner:
    def __init__(
        self,
        strategy: BaseStrategy,
        candles: pd.DataFrame,
        funding: pd.DataFrame | None,
        config: RunnerConfig,
        *,
        gateway: PreTradeRiskGateway,
        order_manager: OrderManager,
        exchange: SimulatedExchange,
        journal: DecisionJournal,
        on_bar=None,  # optional async hook(bar_index, ts) — pacing/snapshots
        entry_cost_gate: CostGate | None = None,
        require_canonical_truth: bool = False,
        record_evaluations: bool = False,
    ) -> None:
        self.strategy = strategy
        self.candles = candles
        self.config = config
        self.on_bar = on_bar
        # Explicit opt-in for separately registered gated research replays.
        # Frozen legacy replays keep their original semantics.
        self.entry_cost_gate = entry_cost_gate
        self.require_canonical_truth = require_canonical_truth
        self.record_evaluations = record_evaluations
        self.gateway = gateway
        self.om = order_manager
        self.execution_context = ExecutionContext.from_runner_mode(
            config.mode,
            live_clock=False,
        )
        self.execution_kernel = build_kernel(
            self.execution_context,
            order_manager,
            AdapterKind.SIMULATED,
        )
        self.exchange = exchange
        self.journal = journal
        self.tracker = PortfolioTracker(exchange, config.starting_equity_usd)
        self.reconciler = PaperReconciler(order_manager, exchange)
        self.replay = MarketReplay(
            candles, funding, symbol=config.symbol,
            spread_bps=config.spread_bps, slippage_est_bps=config.slippage_est_bps,
        )
        # counters
        self.signals = self.orders_submitted = self.risk_rejects = 0
        self.sizing_skips = self.shadow_approved = self.shadow_rejected = 0
        self.recon_mismatches = 0
        self._reconciliation_fail_closed = False
        self._exit_config = ExitEngineConfig(
            trail_atr_mult=config.trail_atr_mult,
            trail_atr_window=config.trail_atr_window,
            max_holding_bars=config.max_holding_bars,
            tick_stops_enabled=config.tick_stops_enabled,
            allow_partial_tp=config.allow_partial_tp,
            fee_aware_breakeven_bps=config.fee_aware_breakeven_bps,
        )

    # --- Helpers -----------------------------------------------------------------
    def _entry_cost_evidence(self, sig: SignalIntent, ref_price: float) -> CostDecisionEvidence | None:
        gate = self.entry_cost_gate
        if gate is None:
            return CostDecisionEvidence.not_evaluated("paper_runner")
        payload = {
            "strategy_id": self.strategy.strategy_id,
            "symbol": self.config.symbol,
            "arm_envelope": sig.decision_envelope.as_dict() if sig.decision_envelope else None,
            "decision_id": sig.decision_envelope.decision_id if sig.decision_envelope else None,
            "cost_profile_id": gate.profile.value,
            "edge_model_id": sig.edge_model_id,
        }
        targets = [x for x in (sig.take_profit_price, *sig.take_profit_levels)
                   if x is not None and math.isfinite(x)]
        rooms = [(x - ref_price) / ref_price * 10_000 * (1 if sig.side == "long" else -1)
                 for x in targets if ref_price > 0]
        favorable = [x for x in rooms if x > 0]
        if sig.expected_gross_edge_bps is None or not favorable:
            self.journal.append("cost_rejected", {**payload, "reason":
                "edge_estimate_missing" if sig.expected_gross_edge_bps is None else "no_favorable_target"})
            return None
        result = gate.evaluate(
            signal_edge_bps=sig.expected_gross_edge_bps, side=sig.side, urgency="taker",
            expected_holding_seconds=int(pd.Timedelta(self.config.timeframe).total_seconds()) * self.config.max_holding_bars,
            symbol=self.config.symbol, current_funding_rate=0,
            available_room_bps=max(favorable),
        )
        self.journal.append("cost_approved" if result.approved else "cost_rejected",
                            {**payload, **result.model_dump(mode="json")})
        return CostDecisionEvidence.from_result(result, profile=gate.profile.value) if result.approved else None

    def _set_quote(self, price: float) -> None:
        bid, ask = quote_from_price(price, self.config.spread_bps)
        self.exchange.set_quote(self.config.symbol, bid, ask)

    def _build_entry_intent(self, sig: SignalIntent, ref_price: float) -> OrderIntent | None:
        sizing = size_position(
            equity_usd=self.tracker.equity_usd(), entry_price=ref_price,
            stop_price=sig.stop_price, side=sig.side,
            config=self.config.risk, limits=self.config.limits,
        )
        if not sizing.approved:
            self.sizing_skips += 1
            logger.info("sizing skipped entry: %s", sizing.reasons)
            self.journal.append("sizing_rejected", {
                "strategy_id": self.strategy.strategy_id, "symbol": self.config.symbol,
                "decision_id": sig.decision_envelope.decision_id if sig.decision_envelope else None,
                "reasons": list(sizing.reasons),
            })
            return None
        return OrderIntent(
            symbol=self.config.symbol, side=sig.side, quantity=sizing.quantity,
            notional_usd=sizing.notional_usd,
            leverage=max(sizing.required_leverage, 1.0),
            reduce_only=False,
        )

    def _seed_plan_from_venue(self, plan: _TradePlan) -> None:
        if plan.order.client_order_id is None:
            return
        status = self.exchange.get_order_status(plan.order.client_order_id)
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

    async def _submit_exit(
        self,
        plan: _TradePlan,
        bar_ts,
        reason: str,
        *,
        quantity: float | None = None,
        final: bool = True,
    ) -> ManagedOrder | None:
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        if pos is None:
            return None
        close_qty = abs(pos.quantity) if quantity is None else min(abs(pos.quantity), quantity)
        if close_qty <= 0:
            return None
        intent = OrderIntent(
            symbol=self.config.symbol,
            side="short" if pos.quantity > 0 else "long",
            quantity=close_qty, notional_usd=0.0, leverage=1.0,
            reduce_only=True,
        )
        evidence = ExecutionEvidence.create(
            strategy_id=f"{self.strategy.strategy_id}:exit:{reason}",
            symbol=self.config.symbol,
            timeframe=self.config.timeframe,
            bar_open=bar_ts.to_pydatetime(),
            side=intent.side,
            htf_snapshot_id=(
                plan.signal.permission_snapshot.snapshot_id
                if plan.signal.permission_snapshot is not None
                else None
            ),
            permission_snapshot=plan.signal.permission_snapshot,
            candle_source="replay",
            entry_clock="exit",
            cost_decision=CostDecisionEvidence.not_evaluated("paper_exit"),
        )
        order = await self.execution_kernel.submit(
            intent, self.tracker.account_state(), self.replay.market_state(self._bar_index),
            evidence=evidence,
            now=bar_ts.to_pydatetime(),
        )
        self.orders_submitted += 1
        self.journal.append("paper_exit", {
            "entry_client_order_id": plan.order.client_order_id,
            "client_order_id": order.client_order_id,
            "entry_decision_id": plan.signal.decision_envelope.decision_id if plan.signal.decision_envelope else None,
            "reason": reason,
            "state": order.state.value,
            "ts": str(bar_ts),
            "quantity": close_qty,
            "final": final,
            "active_stop_price": plan.exit_state.current_stop,
            "breakeven_armed": plan.exit_state.breakeven_armed,
        })
        return order

    def _active_exit_decision(
        self,
        plan: _TradePlan,
        *,
        bar: pd.Series,
        bars_held: int,
        atr: float = 0.0,
    ) -> ActiveExitDecision | None:
        positions = {p.symbol: p for p in self.exchange.get_positions()}
        pos = positions.get(self.config.symbol)
        if pos is None:
            return ActiveExitDecision(
                reason="flat_position",
                exit_price=float(bar["close"]),
                quantity=None,
                final=True,
                active_stop_price=plan.exit_state.current_stop,
                breakeven_armed=plan.exit_state.breakeven_armed,
            )
        return ExitEngine(plan.exit_state, self._exit_config).on_bar(
            high=float(bar["high"]),
            low=float(bar["low"]),
            close=float(bar["close"]),
            position_quantity=abs(pos.quantity),
            min_qty=self.config.limits.min_qty,
            qty_step=self.config.limits.qty_step,
            bars_held=bars_held,
            atr=atr,
        )

    def _strategy_exit_decision(
        self,
        plan: _TradePlan,
        *,
        df: pd.DataFrame,
        index: int,
        bar: pd.Series,
    ) -> ActiveExitDecision | None:
        entry = plan.exit_state.entry_price or plan.order.intent.notional_usd / max(
            plan.order.intent.quantity, 1e-12
        )
        exit_sig = self.strategy.exit_signal(df, index, plan.signal.side, entry)
        if exit_sig is None:
            return None
        return ExitEngine(plan.exit_state, self._exit_config).on_strategy_exit(
            reason=exit_sig.reason,
            price=(
                float(exit_sig.exit_price)
                if exit_sig.exit_price is not None
                else float(bar["close"])
            ),
        )

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

    def _reconcile(self, resolved_plans: dict[str, _TradePlan]) -> _TradePlan | None:
        """Run reconciliation; activate/discard parked plans; return the plan
        that became active (if its entry turned out FILLED)."""
        report = self.reconciler.run()
        self.recon_mismatches += len(report.mismatches)
        self._fail_closed_on_reconciliation(report.mismatches)
        activated: _TradePlan | None = None
        for coid in report.resolved_orders:
            plan = resolved_plans.pop(coid, None)
            if plan is None:
                continue
            state = self.om.orders[coid].state
            if state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED,
                         OrderState.ACKNOWLEDGED):
                activated = plan
            # REJECTED/CANCELLED: lost submission — plan simply dissolves.
        return activated

    # --- Main loop -----------------------------------------------------------------
    async def run(self) -> RunReport:
        cfg = self.config
        df = self.strategy.prepare(self.candles).reset_index(drop=True)
        trail_atr = (
            _atr_indicator(self.candles, cfg.trail_atr_window).reset_index(drop=True)
            if cfg.trail_atr_mult > 0.0
            else None
        )
        n = len(df)
        start = max(self.strategy.warmup_bars, 1)

        pending_signal: SignalIntent | None = None
        pending_decision_ts: pd.Timestamp | None = None
        plan: _TradePlan | None = None
        parked: dict[str, _TradePlan] = {}  # TIMEOUT_UNKNOWN entries by coid
        equities: list[float] = []

        for j in range(start, n):
            self._bar_index = j
            bar = df.iloc[j]
            ts = bar["timestamp"]

            # 1) quote at bar open; tracker rolls the bar
            self._set_quote(float(bar["open"]))
            market = self.replay.market_state(j)
            self.tracker.on_bar(ts)

            # 2) fill last bar's signal at this bar's open
            if pending_signal is not None and self.require_canonical_truth:
                expected_open = pending_decision_ts + pd.Timedelta(self.config.timeframe)
                if pd.Timestamp(ts) != expected_open:
                    self.journal.append("entry_clock_rejected", {
                        "reason": "next_open_missing", "expected_open": expected_open.isoformat(),
                        "actual_open": pd.Timestamp(ts).isoformat(),
                        "decision_id": pending_signal.decision_envelope.decision_id,
                    })
                    pending_signal = None
                    pending_decision_ts = None
                else:
                    self.journal.append("entry_clock_confirmed", {
                        "clock": "next_open", "actual_open": pd.Timestamp(ts).isoformat(),
                        "decision_id": pending_signal.decision_envelope.decision_id,
                    })
            if pending_signal is not None and plan is None and not parked:
                ref_price = float(bar["open"])
                if self.entry_cost_gate is not None:
                    bid, ask = quote_from_price(ref_price, cfg.spread_bps)
                    ref_price = ask if pending_signal.side == "long" else bid
                cost_evidence = self._entry_cost_evidence(pending_signal, ref_price)
                intent = self._build_entry_intent(pending_signal, ref_price) if cost_evidence else None
                if intent is not None:
                    decision_ts = pending_decision_ts or pd.Timestamp(ts)
                    if pending_signal.decision_envelope is None:
                        self.journal.append(
                            "entry_evidence_rejected",
                            {
                                "strategy_id": self.strategy.strategy_id,
                                "symbol": cfg.symbol,
                                "bar_ts": decision_ts.isoformat(),
                                "reason": "decision_envelope_missing",
                            },
                        )
                        pending_signal = None
                        pending_decision_ts = None
                        continue
                    evidence = ExecutionEvidence.from_decision(
                        pending_signal.decision_envelope,
                        cost_decision=cost_evidence,
                    )
                    order = await self.execution_kernel.submit(
                        intent,
                        self.tracker.account_state(),
                        market,
                        evidence=evidence,
                        now=ts.to_pydatetime(),
                    )
                    if order.state is OrderState.RISK_REJECTED:
                        self.risk_rejects += 1
                        if cfg.mode is RunnerMode.SHADOW:
                            self.shadow_rejected += 1
                    else:
                        self.orders_submitted += 1
                        if cfg.mode is RunnerMode.SHADOW:
                            self.shadow_approved += 1
                        exit_state = ExitEngine.from_signal(
                            pending_signal,
                            config=self._exit_config,
                        ).state
                        new_plan = _TradePlan(pending_signal, order, j, exit_state)
                        self._seed_plan_from_venue(new_plan)
                        if order.state is OrderState.TIMEOUT_UNKNOWN:
                            if order.client_order_id is not None:
                                parked[order.client_order_id] = new_plan
                        elif order.state is OrderState.ACKNOWLEDGED:
                            plan = new_plan
            pending_signal = None
            pending_decision_ts = None

            # 3) exit management (paper mode) — stop first, always
            if plan is not None:
                exit_decision = self._active_exit_decision(
                    plan,
                    bar=bar,
                    bars_held=j - plan.entry_bar,
                    atr=(
                        float(trail_atr.iloc[j])
                        if trail_atr is not None and trail_atr.iloc[j] == trail_atr.iloc[j]
                        else 0.0
                    ),
                )
                if exit_decision is None:
                    exit_decision = self._strategy_exit_decision(
                        plan, df=df, index=j, bar=bar
                    )
                if exit_decision is not None:
                    if exit_decision.reason == "flat_position":
                        plan = None
                    else:
                        self._set_quote(exit_decision.exit_price)  # fill at trigger level
                        exit_order = await self._submit_exit(
                            plan,
                            ts,
                            exit_decision.reason,
                            quantity=exit_decision.quantity,
                            final=exit_decision.final,
                        )
                        if (
                            exit_order is not None
                            and exit_order.state in _EXIT_ACCEPTED_STATES
                        ):
                            ExitEngine(plan.exit_state, self._exit_config).mark_fill(
                                exit_decision
                            )
                            if exit_decision.final:
                                plan = None

            # 4) mark to close; new signal only when flat and nothing parked
            self._set_quote(float(bar["close"]))
            if (
                plan is None and not parked and pending_signal is None
                and j < n - 1
            ):
                sig = self.strategy.signal(df, j)
                if self.record_evaluations:
                    from vnedge.strategy.scanner_observability import enrich_evaluation

                    explain = getattr(self.strategy, "evaluation_diagnostics", None)
                    diagnostics = explain(df, j) if callable(explain) else {}
                    self.journal.append("lane_eval", enrich_evaluation({
                        **diagnostics, "strategy_id": self.strategy.strategy_id,
                        "symbol": cfg.symbol, "timeframe": cfg.timeframe,
                        "bar_ts": pd.Timestamp(ts).isoformat(),
                        "decision_at": (pd.Timestamp(ts) + pd.Timedelta(cfg.timeframe)).isoformat(),
                        "fired": sig is not None, "backfill": False,
                        "scope": "offline_paper_replay",
                        "mreg_ready": bool(bar.get("mreg_ready") == 1) if "mreg_ready" in bar else None,
                        "structure_ready": bool(bar.get("bos15_structure_ready") == 1) if "bos15_structure_ready" in bar else None,
                        "data_source": {"candle_source": bar.get("candle_source"),
                                        "decision_row_sha256": bar.get("content_sha256")},
                    }))
                if sig is not None:
                    decision_row = bar.to_dict()
                    decision_row.setdefault("candle_source", "research_replay")
                    try:
                        sig = bind_signal_decision(
                            sig,
                            strategy_id=self.strategy.strategy_id,
                            symbol=cfg.symbol,
                            timeframe=cfg.timeframe,
                            decision_row=decision_row,
                            entry_clock=f"next_{cfg.timeframe}_open",
                            require_existing_snapshot=bool(
                                getattr(
                                    self.strategy,
                                    "requires_permission_snapshot",
                                    False,
                                )
                            ),
                            require_canonical_truth=self.require_canonical_truth,
                        )
                    except (TypeError, ValueError) as exc:
                        self.journal.append(
                            "entry_evidence_rejected",
                            {
                                "strategy_id": self.strategy.strategy_id,
                                "symbol": cfg.symbol,
                                "bar_ts": str(ts),
                                "reason": str(exc),
                            },
                        )
                        sig = None
                    if sig is not None:
                        self.signals += 1
                        pending_signal = sig
                        pending_decision_ts = pd.Timestamp(ts)
                        assert sig.decision_envelope is not None
                        self.journal.append(
                            "decision_armed",
                            sig.decision_envelope.as_dict(),
                        )

            # 5) periodic reconciliation
            if (j - start) % cfg.reconcile_every_bars == 0 or parked:
                activated = self._reconcile(parked)
                if activated is not None and plan is None:
                    self._seed_plan_from_venue(activated)
                    plan = activated

            equities.append(self.tracker.equity_usd())
            if self.on_bar is not None:
                await self.on_bar(j, ts)

        # final reconciliation
        self._reconcile(parked)

        peak, max_dd = 0.0, 0.0
        for eq in equities:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * 100.0)

        fills = self.exchange.get_fills()
        report = RunReport(
            mode=cfg.mode.value, symbol=cfg.symbol,
            strategy_id=self.strategy.strategy_id,
            bars_processed=n - start, signals_generated=self.signals,
            orders_submitted=self.orders_submitted, fills=len(fills),
            fees_usd=sum(f.fee_usd for f in fills),
            realized_pnl_usd=self.exchange.get_balances()["USDT"]
            - cfg.starting_equity_usd,
            unrealized_pnl_usd=self.tracker.unrealized_pnl_usd(),
            max_drawdown_pct=max_dd,
            risk_rejects=self.risk_rejects, sizing_skips=self.sizing_skips,
            shadow_approved=self.shadow_approved,
            shadow_rejected=self.shadow_rejected,
            reconciliation_mismatches=self.recon_mismatches,
            final_equity_usd=self.tracker.equity_usd(),
        )
        self.journal.append("run_report", report.to_dict())
        logger.info(report.summary)
        return report
