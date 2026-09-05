"""Durable shadow runner for ``squeeze_expansion_breakout_v3``."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import cast

import pandas as pd

from vnedge.exchange.book_imbalance import BookImbalance
from vnedge.execution.evidence import (
    CostDecisionEvidence,
    DecisionEnvelope,
    ExecutionEvidence,
)
from vnedge.execution.exit_engine import ExitConfig, ExitDecision, ExitEngine
from vnedge.execution.trigger_engine import FireDecision, Side
from vnedge.runtime.conversion_taxonomy import conversion_reject_category
from vnedge.runtime.execution_contract import KERNEL_PATH_ID
from vnedge.runtime.expansion_acceptance import CompressionArm, ExpansionAcceptanceEngine
from vnedge.runtime.funding_ledger import FundingPrint, funding_cost_usd
from vnedge.runtime.latency_tracker import (
    ACCEPTANCE_HOLD_MS,
    SHADOW_JOURNAL_MS,
    TICK_STOP_MS,
    LatencyTracker,
)
from vnedge.runtime.scanner_session import SessionCosts
from vnedge.runtime.squeeze_observe import FireGuard, JournalSink, ScannerApproval
from vnedge.strategy.arm_evidence import (
    FrozenPermissionSnapshot,
    MissingHtfContext,
    freeze_permission_from_row,
)
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.realtime_entry import RealtimeEntryArm
from vnedge.strategy.squeeze_expansion_breakout_v3 import PARAMS


@dataclass
class SqueezeAcceptanceObserveRunner:
    """Closed bars arm levels; current quote samples accept and price entries."""

    journal: JournalSink
    symbol: str
    strategy_id: str = "squeeze_expansion_breakout_v3"
    strategy: BaseStrategy | None = None
    notional_usd: float = 3000.0
    margin_usd: float = 100.0
    approve_fire: FireGuard | None = None
    acceptance: ExpansionAcceptanceEngine = field(default_factory=ExpansionAcceptanceEngine)
    exits: ExitEngine = field(
        default_factory=lambda: ExitEngine(
            config=ExitConfig(
                breakeven_cost_bps=PARAMS.round_trip_cost_bps,
                be_fee_buffer_bps=PARAMS.breakeven_buffer_bps,
            )
        )
    )
    costs: SessionCosts = field(default_factory=lambda: SessionCosts.from_profile("delta_scalp"))
    decision_timeframe: str = "5m"
    context_timeframes: tuple[str, ...] = ()
    latency: LatencyTracker | None = None
    open_meta: dict | None = None
    candidates: int = 0
    fires: int = 0
    rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    conversion_rejections: dict[str, int] = field(default_factory=dict)
    outcomes: int = 0
    net_usd: float = 0.0
    current_bar_index: int = -1
    last_approval: ScannerApproval | None = None
    _restore_payload: dict | None = field(default=None, init=False, repr=False)
    _restore_error: str | None = field(default=None, init=False, repr=False)
    _last_journaled_acceptance: str | None = field(default=None, init=False, repr=False)
    _last_journaled_episode: int | None = field(default=None, init=False, repr=False)
    _journaled_quote_diagnostics: set[tuple[int | None, str]] = field(
        default_factory=set, init=False, repr=False
    )
    _armed_episodes: set[tuple[str, int]] = field(default_factory=set, init=False, repr=False)
    _last_bid: float | None = field(default=None, init=False, repr=False)
    _last_ask: float | None = field(default=None, init=False, repr=False)

    @property
    def intent_prefix(self) -> str:
        return (
            "squeeze_acceptance_v3"
            if self.strategy_id == "squeeze_expansion_breakout_v3"
            else self.strategy_id
        )

    def __post_init__(self) -> None:
        if self.costs.cost_model is not None:
            self.exits = ExitEngine(
                config=replace(
                    self.exits.config,
                    breakeven_cost_bps=self.costs.cost_model.round_trip_bps(include_safety=False),
                    be_fee_buffer_bps=PARAMS.breakeven_buffer_bps,
                )
            )
        read_all = getattr(self.journal, "read_all", None)
        if not callable(read_all):
            return
        intents: dict[str, dict] = {}
        resolved: set[str] = set()
        funding_by_intent: dict[str, tuple[float, set[str]]] = {}
        for record in read_all():
            payload = record.get("payload", {})
            key = str(payload.get("intent_key") or "")
            belongs_to_runner = (
                str(payload.get("strategy_id") or "") == self.strategy_id
                and str(payload.get("symbol") or "") == self.symbol
            ) or (
                key.startswith(f"{self.intent_prefix}|")
                and f"|{self.symbol}|" in key
            )
            if (
                record.get("kind") == "scanner_transition"
                and str(payload.get("strategy_id") or "") == self.strategy_id
                and str(payload.get("state") or "").startswith("armed_")
                and payload.get("episode_id") is not None
            ):
                self._armed_episodes.add(
                    (str(payload.get("symbol") or self.symbol), int(payload["episode_id"]))
                )
            if record.get("kind") == "shadow_intent" and belongs_to_runner:
                self.candidates += 1
                if payload.get("approved"):
                    intents[key] = payload
                    self.fires += 1
                else:
                    self.rejected += 1
                    self._count_rejection(payload.get("failed_checks"))
            elif record.get("kind") == "shadow_outcome" and belongs_to_runner:
                resolved.add(key)
                self.outcomes += 1
                self.net_usd += float(payload.get("virtual_net_usd") or 0.0)
            elif (
                record.get("kind") == "funding_applied"
                and payload.get("book") == "quote_shadow"
                and belongs_to_runner
            ):
                cost, event_ids = funding_by_intent.get(key, (0.0, set()))
                event_id = str(payload.get("funding_event_id") or "")
                if event_id and event_id not in event_ids:
                    event_ids.add(event_id)
                    cost += float(payload.get("funding_cost_usd") or 0.0)
                funding_by_intent[key] = (cost, event_ids)
        pending = [payload for key, payload in intents.items() if key not in resolved]
        if len(pending) > 1:
            self._restore_error = "multiple unresolved v3 scanner intents"
        elif pending:
            self._restore_payload = dict(pending[0])
            pending_key = str(self._restore_payload.get("intent_key") or "")
            funding_cost, event_ids = funding_by_intent.get(pending_key, (0.0, set()))
            self._restore_payload["funding_cost_usd"] = funding_cost
            self._restore_payload["funding_event_ids"] = sorted(event_ids)

    def _count_rejection(self, failed_checks: object) -> None:
        checks = (
            [str(value) for value in failed_checks]
            if isinstance(failed_checks, (list, tuple))
            else [str(failed_checks)]
            if failed_checks
            else ["unreported"]
        )
        if any(value.startswith("cost_gate:") for value in checks):
            bucket = "cost"
        elif any(value.startswith("sizing:") for value in checks):
            bucket = "sizing"
        elif any(value.startswith("shadow_portfolio:") for value in checks):
            bucket = "portfolio"
        elif any(
            value.startswith(("candle_path:", "daily_factory", "protection"))
            for value in checks
        ):
            bucket = "prerequisite"
        else:
            bucket = "risk"
        self.rejection_reasons[bucket] = self.rejection_reasons.get(bucket, 0) + 1
        category = conversion_reject_category(checks)
        self.conversion_rejections[category] = (
            self.conversion_rejections.get(category, 0) + 1
        )

    def restore(self, df: pd.DataFrame) -> None:
        """Rebuild arm state and at most one durable virtual position."""
        self._prepared_frame = df
        timestamps = pd.to_datetime(df.get("timestamp"), utc=True)
        pending = self._restore_payload
        entry_index: int | None = None
        decision_ts: pd.Timestamp | None = None
        if pending is not None:
            decision_ts = pd.Timestamp(pending.get("bar_ts"))
            if decision_ts.tzinfo is None:
                decision_ts = decision_ts.tz_localize("UTC")
            else:
                decision_ts = decision_ts.tz_convert("UTC")
            # Quote entries belong to the candle that was FORMING at the
            # decision timestamp.  Mapping to the previous closed candle made
            # its already-observed low/high eligible to stop a future entry.
            entry_open = decision_ts.floor(f"{int(self.costs.bar_minutes)}min")
            exact = [i for i, ts in enumerate(timestamps) if ts == entry_open]
            if exact:
                entry_index = exact[-1]
            else:
                prior = [i for i, ts in enumerate(timestamps) if ts < entry_open]
                entry_index = (prior[-1] + 1) if prior else 0
            if entry_index < 0:
                self._restore_error = "v3 scanner decision bar absent from warmup"
                return

        # ``prepare()`` has already computed the complete causal feature frame.
        # Replaying every prepared row here is both unnecessary and expensive:
        # a flat quote scanner can only retain an arm for its short grace
        # window.  Historical arm transitions are restored from the journal in
        # ``__post_init__``.  An open durable position is different: replay its
        # exit path from the entry candle so stop/trail/strategy exits remain
        # restart-equivalent.
        if pending is not None and entry_index is not None:
            replay_start = min(entry_index, len(df))
        else:
            arm_tail_bars = max(4, int(self.acceptance.config.arm_grace_bars) + 1)
            replay_start = max(0, len(df) - arm_tail_bars)

        for index in range(replay_start, len(df)):
            self.current_bar_index = index
            self._update_arm_from_row(df.iloc[index], index)
            if pending is not None and index == entry_index:
                assert decision_ts is not None
                intent = pending.get("intent") or {}
                side = str(intent.get("side") or "")
                if side not in {"long", "short"}:
                    self._restore_error = "v3 scanner restore has invalid side"
                    return
                self.exits.open_from_fire(
                    side=cast(Side, side),
                    entry=float(pending["entry_price"]),
                    stop=float(pending["stop_price"]),
                    risk=float(pending["risk"]),
                    box_edge=float(pending["box_edge"]),
                    entry_bar=index,
                )
                self.acceptance.position_open = True
                self.acceptance.active_side = cast(Side, side)
                self.open_meta = {
                    "side": side,
                    "entry": float(pending["entry_price"]),
                    "entry_bar": index,
                    "intent_key": pending.get("intent_key"),
                    "reason": pending.get("signal_reason", ""),
                    "bar_ts": decision_ts.to_pydatetime(),
                    "entry_ts": decision_ts.to_pydatetime(),
                    "notional_usd": float(intent.get("notional_usd") or self.notional_usd),
                    "margin_usd": float(pending.get("margin_usd") or self.margin_usd),
                    "funding_cost_usd": float(pending.get("funding_cost_usd") or 0.0),
                    "funding_event_ids": set(pending.get("funding_event_ids") or ()),
                    "arm_evidence": pending.get("arm_evidence"),
                    "arm_envelope": pending.get("arm_envelope"),
                    "execution_evidence": pending.get("execution_evidence"),
                }
                self._restore_payload = None
            if self.open_meta is not None and entry_index is not None and index >= entry_index:
                row = df.iloc[index]
                decision = self.exits.on_bar(
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    atr=self._row_atr(row),
                    bar_index=index,
                    partial_entry_bar=index == entry_index,
                )
                decision = decision or self._strategy_exit(df, index, row)
                if decision is not None:
                    net_won = self._journal_outcome(
                        decision, index, timestamps.iloc[index].to_pydatetime()
                    )
                    self.acceptance.notify_flat(bar_index=index, net_won=net_won)
                    self.open_meta = None

        # The entry candle can still be forming and therefore absent from the
        # closed-bar frame.  Restore the durable position at its future index;
        # the next close will be processed as a partial entry candle.
        if pending is not None and entry_index == len(df):
            intent = pending.get("intent") or {}
            side = str(intent.get("side") or "")
            if side not in {"long", "short"} or decision_ts is None:
                self._restore_error = "v3 scanner restore has invalid side"
                return
            self.exits.open_from_fire(
                side=cast(Side, side),
                entry=float(pending["entry_price"]),
                stop=float(pending["stop_price"]),
                risk=float(pending["risk"]),
                box_edge=float(pending["box_edge"]),
                entry_bar=entry_index,
            )
            self.acceptance.position_open = True
            self.acceptance.active_side = cast(Side, side)
            self.open_meta = {
                "side": side,
                "entry": float(pending["entry_price"]),
                "entry_bar": entry_index,
                "intent_key": pending.get("intent_key"),
                "reason": pending.get("signal_reason", ""),
                "bar_ts": decision_ts.to_pydatetime(),
                "entry_ts": decision_ts.to_pydatetime(),
                "notional_usd": float(intent.get("notional_usd") or self.notional_usd),
                "margin_usd": float(pending.get("margin_usd") or self.margin_usd),
                "funding_cost_usd": float(pending.get("funding_cost_usd") or 0.0),
                "funding_event_ids": set(pending.get("funding_event_ids") or ()),
                "arm_evidence": pending.get("arm_evidence"),
            }
            self._restore_payload = None

    def on_closed_bar(self, df: pd.DataFrame, index: int, bar_ts: datetime) -> None:
        """Consume one causal closed-bar event.

        This is the canonical engine method used by both live and recorded
        replay. ``on_prepared_bar`` remains a compatibility delegate while
        callers migrate; it contains no independent scanner logic.
        """
        self._prepared_frame = df
        self.current_bar_index = index
        row = df.iloc[index]
        if self.open_meta is not None:
            entry_bar = int(self.open_meta.get("entry_bar", index))
            decision = self.exits.on_bar(
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                atr=self._row_atr(row),
                bar_index=index,
                partial_entry_bar=index == entry_bar,
            )
            decision = decision or self._strategy_exit(df, index, row)
            if decision is not None:
                net_won = self._journal_outcome(decision, index, bar_ts)
                self.acceptance.notify_flat(bar_index=index, net_won=net_won)
                self.open_meta = None
        self._update_arm_from_row(row, index, journal_event_ts=bar_ts)

    def on_prepared_bar(self, df: pd.DataFrame, index: int, bar_ts: datetime) -> None:
        """Compatibility alias; all behavior lives in :meth:`on_closed_bar`."""
        self.on_closed_bar(df, index, bar_ts)

    def on_quote(
        self,
        *,
        bid: float,
        ask: float,
        ts: datetime,
        received_ts: datetime | None = None,
        sequence: int | str | None = None,
        source: str = "unknown",
        exchange_timestamped: bool = False,
        overflow_drops: int = 0,
        book: BookImbalance | None = None,
    ) -> FireDecision | None:
        if math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask:
            self._last_bid = bid
            self._last_ask = ask
        if self._restore_error is not None:
            return None
        hold_observation = self.acceptance.hold_observation_id
        self.acceptance.note_quote_overflow(overflow_drops, observed_at=ts)
        if self.open_meta is not None:
            pos = self.exits.pos
            price = bid if pos is not None and pos.side == "long" else ask
            tick_started = time.perf_counter()
            try:
                decision = self.exits.on_tick(price=price)
            finally:
                if self.latency is not None:
                    self.latency.record(TICK_STOP_MS, (time.perf_counter() - tick_started) * 1000.0)
            if decision is not None:
                net_won = self._journal_outcome(decision, self.current_bar_index, ts)
                self.acceptance.notify_flat(bar_index=self.current_bar_index, net_won=net_won)
                self.open_meta = None
            return None

        fire = self.acceptance.observe_quote(
            bid=bid,
            ask=ask,
            ts=ts,
            bar_index=self.current_bar_index,
            received_ts=received_ts,
            sequence=sequence,
            source=source,
            exchange_timestamped=exchange_timestamped,
            book=book,
        )
        if (
            self.latency is not None
            and self.acceptance.hold_observation_id != hold_observation
            and self.acceptance.last_hold_ms is not None
        ):
            self.latency.record(ACCEPTANCE_HOLD_MS, self.acceptance.last_hold_ms)
        self._journal_acceptance_transition(
            bid=bid,
            ask=ask,
            ts=ts,
            received_ts=received_ts or ts,
            sequence=sequence,
            source=source,
            exchange_timestamped=exchange_timestamped,
        )
        if fire is None:
            return None
        self.candidates += 1
        arm = self.acceptance.arm
        decision = arm.decision_for(fire.side) if arm is not None else None
        if decision is None:
            self.rejected += 1
            self._count_rejection(("decision_envelope_missing",))
            self.acceptance.last_reason = "decision_envelope_missing"
            return None
        arm_evidence = arm.evidence.as_dict() if arm is not None and arm.evidence else None
        accepted_evidence = ExecutionEvidence.from_decision(
            decision,
            quote_sequence=sequence,
            bbo_ts=ts,
            quote_age_ms=max(
                0.0,
                ((received_ts or ts) - ts).total_seconds() * 1000.0,
            ),
            cost_decision=CostDecisionEvidence.not_evaluated("accepted_before_approval"),
        )
        self.journal.append(
            "decision_accepted",
            {
                "intent_key": decision.decision_id,
                "decision_id": decision.decision_id,
                "path_id": decision.path_id,
                "strategy_id": self.strategy_id,
                "symbol": self.symbol,
                "arm_envelope": decision.as_dict(),
                "execution_evidence": accepted_evidence.as_dict(),
                "quote_sequence": sequence,
                "bbo_ts": ts.isoformat(),
                "quote_received_ts": (received_ts or ts).isoformat(),
                "quote_age_ms": accepted_evidence.quote_age_ms,
                "performance_eligible": False,
            },
        )
        approval = (
            self.approve_fire(fire, self.current_bar_index, ts)
            if self.approve_fire is not None
            else ScannerApproval(
                approved=True,
                intent={
                    "symbol": self.symbol,
                    "side": fire.side,
                    "notional_usd": self.notional_usd,
                    "strategy_id": self.strategy_id,
                    "order_type": "shadow_quote_acceptance",
                },
                passed_checks=("quote_acceptance",),
                explanation=fire.reason,
                notional_usd=self.notional_usd,
                margin_usd=self.margin_usd,
                intent_key=decision.decision_id,
                execution_evidence=ExecutionEvidence.from_decision(
                    decision,
                    quote_sequence=sequence,
                    bbo_ts=ts,
                    quote_age_ms=max(
                        0.0,
                        ((received_ts or ts) - ts).total_seconds() * 1000.0,
                    ),
                    cost_decision=CostDecisionEvidence.not_evaluated(
                        "observe_without_gateway"
                    ),
                ).as_dict(),
            )
        )
        if approval.intent_key and approval.intent_key != decision.decision_id:
            self.rejected += 1
            self._count_rejection(("decision_identity_mismatch",))
            self.acceptance.last_reason = "decision_identity_mismatch"
            return None
        if not approval.intent_key:
            approval = replace(
                approval,
                intent_key=decision.decision_id,
                execution_evidence=(
                    approval.execution_evidence
                    or ExecutionEvidence.from_decision(
                        decision,
                        quote_sequence=sequence,
                        bbo_ts=ts,
                        quote_age_ms=max(
                            0.0,
                            ((received_ts or ts) - ts).total_seconds() * 1000.0,
                        ),
                        cost_decision=CostDecisionEvidence.not_evaluated(
                            "rejected_before_cost_or_risk"
                        ),
                    ).as_dict()
                ),
            )
        self.last_approval = approval
        key = decision.decision_id
        journal_started = time.perf_counter()
        try:
            self.journal.append(
                "shadow_intent",
                {
                    "intent_key": key,
                    "decision_id": key,
                    "strategy_id": self.strategy_id,
                    "symbol": self.symbol,
                    "approved": approval.approved,
                    "failed_checks": list(approval.failed_checks),
                    "passed_checks": list(approval.passed_checks),
                    "explanation": approval.explanation or fire.reason,
                    "intent": approval.intent,
                    "execution_evidence": approval.execution_evidence,
                    "performance_eligible": False,
                    "signal_reason": fire.reason,
                    "entry_price": fire.entry,
                    "stop_price": fire.stop,
                    "box_edge": fire.box_edge,
                    "risk": fire.risk,
                    "quote_event_ts": ts.isoformat(),
                    "quote_received_ts": (received_ts or ts).isoformat(),
                    "quote_sequence": sequence,
                    "quote_source": source,
                    "quote_exchange_timestamped": exchange_timestamped,
                    "quote_ingest_lag_seconds": self.acceptance.last_quote_lag_seconds,
                    "episode_id": fire.episode_id,
                    "arm_evidence": arm_evidence,
                    "arm_envelope": decision.as_dict(),
                    "margin_usd": approval.margin_usd or self.margin_usd,
                    "take_profit_price": None,
                    "take_profit_levels": [],
                    "bar_ts": ts.isoformat(),
                    "acceptance": "quote_hold",
                },
            )
        finally:
            if self.latency is not None:
                self.latency.record(
                    SHADOW_JOURNAL_MS,
                    (time.perf_counter() - journal_started) * 1000.0,
                )
        if not approval.approved:
            self.rejected += 1
            self._count_rejection(approval.failed_checks)
            self.acceptance.notify_rejected()
            return fire
        self.exits.open_from_fire(
            side=fire.side,
            entry=fire.entry,
            stop=fire.stop,
            risk=fire.risk,
            box_edge=fire.box_edge,
            # The last index is a closed candle.  This quote belongs to the
            # next, currently-forming candle.
            entry_bar=self.current_bar_index + 1,
        )
        self.open_meta = {
            "side": fire.side,
            "entry": fire.entry,
            "entry_bar": self.current_bar_index + 1,
            "intent_key": key,
            "reason": fire.reason,
            "bar_ts": ts,
            "entry_ts": ts,
            "notional_usd": approval.notional_usd or self.notional_usd,
            "margin_usd": approval.margin_usd or self.margin_usd,
            "funding_cost_usd": 0.0,
            "funding_event_ids": set(),
            "arm_evidence": arm_evidence,
            "arm_envelope": decision.as_dict(),
            "execution_evidence": approval.execution_evidence,
        }
        self.fires += 1
        return fire

    def apply_funding_print(self, event: FundingPrint) -> bool:
        """Book one settled venue funding print against an open virtual position.

        Funding never creates, delays, or expires an arm.  It is an idempotent
        inventory cash flow applied only after the quote-accepted entry exists.
        """
        meta = self.open_meta
        if meta is None:
            return False
        entry_ts = meta.get("entry_ts") or meta.get("bar_ts")
        if isinstance(entry_ts, datetime) and event.ts_ms <= int(entry_ts.timestamp() * 1000):
            return False
        event_ids = meta.setdefault("funding_event_ids", set())
        if not isinstance(event_ids, set):
            event_ids = set(event_ids)
            meta["funding_event_ids"] = event_ids
        if event.event_id in event_ids:
            return False
        side = str(meta.get("side") or "")
        if side not in {"long", "short"}:
            return False
        notional = float(meta.get("notional_usd") or self.notional_usd)
        cost = funding_cost_usd(side=side, notional_usd=notional, rate=event.rate)
        event_ids.add(event.event_id)
        meta["funding_cost_usd"] = float(meta.get("funding_cost_usd") or 0.0) + cost
        self.journal.append(
            "funding_applied",
            {
                "book": "quote_shadow",
                "intent_key": meta.get("intent_key"),
                "decision_id": meta.get("intent_key"),
                "path_id": KERNEL_PATH_ID,
                "strategy_id": self.strategy_id,
                "symbol": self.symbol,
                "funding_event_id": event.event_id,
                "funding_ts_ms": event.ts_ms,
                "funding_rate": event.rate,
                "funding_cost_usd": cost,
                "cash_delta_usd": -cost,
                "source": event.source,
            },
        )
        return True

    def _journal_acceptance_transition(
        self,
        *,
        bid: float,
        ask: float,
        ts: datetime,
        received_ts: datetime,
        sequence: int | str | None,
        source: str,
        exchange_timestamped: bool,
    ) -> None:
        reason = self.acceptance.last_reason
        arm = self.acceptance.arm
        episode = arm.episode_id if arm is not None else None

        # These are per-quote diagnostics, not lifecycle transitions.  A
        # combined ticker/L1 feed legitimately alternates ``quote_duplicate``
        # and ``no_active_arm`` while flat.  Persisting every alternation used
        # to fsync several records per second per lane, growing journals to
        # ~1 GiB and starving the candle/event loop until the safety latency
        # gate blocked every new arm.  Keep the first observation per arm
        # episode (or once while flat); cumulative counters remain present on
        # every real arm/probe/accept transition and in the runtime snapshot.
        diagnostic_reasons = {
            "invalid_quote",
            "quote_clock_skew",
            "quote_ingest_lag",
            "quote_out_of_order",
            "quote_duplicate",
            "no_active_arm",
            "quote_outside_session",
            "one_net_position",
            "cooldown",
            "daily_fire_budget",
        }
        if reason in diagnostic_reasons:
            diagnostic_key = (episode, reason)
            if diagnostic_key in self._journaled_quote_diagnostics:
                return
            self._journaled_quote_diagnostics.add(diagnostic_key)
        if reason == self._last_journaled_acceptance:
            return
        self._last_journaled_acceptance = reason
        self._last_journaled_episode = episode
        self.journal.append(
            "scanner_transition",
            {
                "strategy_id": self.strategy_id,
                "symbol": self.symbol,
                "state": reason,
                "event_ts": ts.isoformat(),
                "received_ts": received_ts.isoformat(),
                "sequence": sequence,
                "source": source,
                "exchange_timestamped": exchange_timestamped,
                "ingest_lag_seconds": self.acceptance.last_quote_lag_seconds,
                "bid": bid,
                "ask": ask,
                "bar_index": self.current_bar_index,
                "episode_id": arm.episode_id if arm is not None else None,
                "long_state": self.acceptance.long.state.value,
                "short_state": self.acceptance.short.state.value,
                "quotes_seen": self.acceptance.quotes_seen,
                "quotes_distinct": self.acceptance.quotes_distinct,
                "quote_contract_rejects": self.acceptance.quote_contract_rejects,
                "book_filter_rejects": self.acceptance.book_filter_rejects,
                "book_imbalance": self.acceptance.last_book_imbalance,
                "book_spread_ticks": self.acceptance.last_book_spread_ticks,
                "quote_overflow_drops": self.acceptance.quote_overflow_drops,
                "quote_rearms": self.acceptance.quote_rearms,
                "overflow_probe_resets": self.acceptance.overflow_probe_resets,
                "arm_evidence": arm.evidence.as_dict() if arm is not None and arm.evidence else None,
                "decision_ids": {
                    item.side: item.decision_id for item in arm.decisions
                } if arm is not None else {},
                "arm_envelopes": [
                    item.as_dict() for item in arm.decisions
                ] if arm is not None else [],
            },
        )

    def _update_arm_from_row(
        self,
        row: pd.Series,
        index: int,
        *,
        journal_event_ts: datetime | None = None,
    ) -> None:
        previous_reason = self.acceptance.last_reason
        if self.strategy is not None:
            # Give a concrete quote-entry strategy its complete causal frame.
            # ``_prepared_frame`` is attached by on_prepared_bar/restore below;
            # falling back to legacy squeeze columns preserves old revisions.
            prepared = getattr(self, "_prepared_frame", None)
            if isinstance(prepared, pd.DataFrame):
                arm = self.strategy.realtime_arm(prepared, index)
                if arm is None and bool(
                    getattr(self.strategy, "requires_permission_snapshot", False)
                ):
                    explain = getattr(self.strategy, "evaluation_diagnostics", None)
                    try:
                        diagnostics = explain(prepared, index) if callable(explain) else {}
                    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                        # Diagnostics must never become a second decision path.
                        # The strategy already refused the arm; an explanation
                        # failure may reduce observability but cannot revive it.
                        diagnostics = {}
                    if (
                        isinstance(diagnostics, dict)
                        and diagnostics.get("primary_failed_gate") == "htf_context_missing"
                    ):
                        self.acceptance.last_reason = "htf_context_missing"
                        self._journal_arm_transition(previous_reason, journal_event_ts)
                        return
                if isinstance(arm, RealtimeEntryArm):
                    try:
                        bound_arm = self._compression_arm(arm, index, row)
                    except MissingHtfContext:
                        self.acceptance.last_reason = "htf_context_missing"
                        self._journal_arm_transition(previous_reason, journal_event_ts)
                        return
                    except (TypeError, ValueError):
                        self.acceptance.last_reason = "permission_evidence_unbound"
                        self._journal_arm_transition(previous_reason, journal_event_ts)
                        return
                    if (
                        bool(getattr(self.strategy, "requires_permission_snapshot", False))
                        and bound_arm.evidence is None
                    ):
                        self.acceptance.last_reason = "htf_context_missing"
                        self._journal_arm_transition(previous_reason, journal_event_ts)
                        return
                    self.acceptance.update_arm(bound_arm)
                    self._journal_arm_transition(previous_reason, journal_event_ts)
                    return
        values = {
            name: float(row.get(name, float("nan")))
            for name in (
                "sqz_episode",
                "sqz_range_high",
                "sqz_range_low",
                "sqz_atr",
                "sqz_vwap24",
                "sqz_compressed",
            )
        }
        if not all(math.isfinite(values[n]) for n in values):
            return
        self.acceptance.update_arm(
            CompressionArm(
                episode_id=int(values["sqz_episode"]),
                box_high=values["sqz_range_high"],
                box_low=values["sqz_range_low"],
                atr=values["sqz_atr"],
                vwap=values["sqz_vwap24"],
                bar_index=index,
                compressed=values["sqz_compressed"] > 0,
                evidence=(evidence := self._freeze_arm_evidence(
                    row,
                    allow_long=True,
                    allow_short=True,
                    reason="squeeze_acceptance_v3",
                )),
                decisions=self._decision_envelopes(evidence),
            )
        )
        self._journal_arm_transition(previous_reason, journal_event_ts)

    def _journal_arm_transition(
        self, previous_reason: str, event_ts: datetime | None
    ) -> None:
        """Persist the close-driven arm state that quote-only logs cannot see."""
        reason = self.acceptance.last_reason
        arm = self.acceptance.arm
        episode = arm.episode_id if arm is not None else None
        if (
            event_ts is None
            or (
                reason == previous_reason
                and reason == self._last_journaled_acceptance
                and episode == self._last_journaled_episode
            )
        ):
            return
        self._last_journaled_acceptance = reason
        self._last_journaled_episode = episode
        if reason.startswith("armed_") and episode is not None:
            self._armed_episodes.add((self.symbol, int(episode)))
        self.journal.append(
            "scanner_transition",
            {
                "strategy_id": self.strategy_id,
                "symbol": self.symbol,
                "state": reason,
                "event_ts": event_ts.isoformat(),
                "received_ts": event_ts.isoformat(),
                "sequence": None,
                "source": "canonical_close",
                "exchange_timestamped": False,
                "bid": self._last_bid,
                "ask": self._last_ask,
                "bar_index": self.current_bar_index,
                "episode_id": episode,
                "long_state": self.acceptance.long.state.value,
                "short_state": self.acceptance.short.state.value,
                "quotes_seen": self.acceptance.quotes_seen,
                "quotes_distinct": self.acceptance.quotes_distinct,
                "quote_contract_rejects": self.acceptance.quote_contract_rejects,
                "book_filter_rejects": self.acceptance.book_filter_rejects,
                "book_imbalance": self.acceptance.last_book_imbalance,
                "book_spread_ticks": self.acceptance.last_book_spread_ticks,
                "quote_overflow_drops": self.acceptance.quote_overflow_drops,
                "quote_rearms": self.acceptance.quote_rearms,
                "overflow_probe_resets": self.acceptance.overflow_probe_resets,
                "arm_evidence": arm.evidence.as_dict() if arm is not None and arm.evidence else None,
                "decision_ids": {
                    item.side: item.decision_id for item in arm.decisions
                } if arm is not None else {},
                "arm_envelopes": [
                    item.as_dict() for item in arm.decisions
                ] if arm is not None else [],
            },
        )

    def _freeze_arm_evidence(
        self,
        row: pd.Series,
        *,
        allow_long: bool,
        allow_short: bool,
        reason: str,
    ) -> FrozenPermissionSnapshot | None:
        if not self.decision_timeframe:
            return None
        builder = getattr(self.strategy, "freeze_permission_snapshot", None)
        if callable(builder):
            return builder(
                row,
                allow_long=allow_long,
                allow_short=allow_short,
                reason=reason,
            )
        return freeze_permission_from_row(
            row.to_dict(),
            decision_timeframe=self.decision_timeframe,
            context_timeframes=self.context_timeframes,
            allow_long=allow_long,
            allow_short=allow_short,
            reason=reason,
        )

    def _compression_arm(
        self,
        arm: RealtimeEntryArm,
        index: int,
        row: pd.Series,
    ) -> CompressionArm:
        evidence = arm.evidence or self._freeze_arm_evidence(
            row,
            allow_long=arm.allow_long,
            allow_short=arm.allow_short,
            reason=arm.reason,
        )
        return CompressionArm(
            episode_id=arm.episode_id,
            box_high=arm.long_level,
            box_low=arm.short_level,
            atr=arm.atr,
            vwap=arm.reference_price,
            bar_index=index,
            compressed=True,
            allow_long=arm.allow_long,
            allow_short=arm.allow_short,
            long_level=arm.long_level,
            short_level=arm.short_level,
            long_structural_stop=arm.long_structural_stop,
            short_structural_stop=arm.short_structural_stop,
            structural_stop_mode=arm.structural_stop_mode,
            expires_after_bars=arm.expires_after_bars,
            session_start_hour_utc=arm.session_start_hour_utc,
            session_end_hour_utc=arm.session_end_hour_utc,
            reason=arm.reason,
            evidence=evidence,
            decisions=self._decision_envelopes(evidence),
        )

    def _decision_envelopes(
        self, evidence: FrozenPermissionSnapshot | None
    ) -> tuple[DecisionEnvelope, ...]:
        """Mint side-specific identities once, on the closed-bar arm."""

        if evidence is None:
            raise ValueError("decision envelope requires frozen closed-bar evidence")
        entry_clock = "quote_hold"
        runtime_contract = getattr(self, "runtime_contract", None)
        if runtime_contract is not None:
            entry_clock = str(runtime_contract.evidence_entry_clock)
        return tuple(
            DecisionEnvelope.create(
                strategy_id=self.strategy_id,
                symbol=self.symbol,
                timeframe=(
                    self.decision_timeframe
                    or evidence.decision_bar.timeframe
                ),
                side=side,
                permission_snapshot=evidence,
                entry_clock=entry_clock,
            )
            for side, allowed in (
                ("long", evidence.allow_long),
                ("short", evidence.allow_short),
            )
            if allowed
        )

    @staticmethod
    def _row_atr(row: pd.Series) -> float:
        for name in ("rt_atr", "sqz_atr", "bos15_bos_atr", "sc15_atr"):
            try:
                value = float(row.get(name, float("nan")))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                return value
        return 0.0

    def _strategy_exit(self, df: pd.DataFrame, index: int, row: pd.Series) -> ExitDecision | None:
        """Apply a causal strategy deterioration only after hard protection."""
        pos = self.exits.pos
        if self.strategy is None or pos is None:
            return None
        exit_intent = self.strategy.exit_signal(df, index, pos.side, pos.entry)
        if exit_intent is None:
            return None
        close = float(row["close"])
        return self.exits.close_now(
            price=(float(exit_intent.exit_price) if exit_intent.exit_price is not None else close),
            reason=exit_intent.reason,
        )

    def _journal_outcome(self, decision: ExitDecision, index: int, bar_ts: datetime) -> bool:
        meta = self.open_meta or {}
        side = str(meta.get("side") or "long")
        entry = float(meta.get("entry") or 1.0)
        gross_bps = (
            (decision.price / entry - 1) if side == "long" else (1 - decision.price / entry)
        ) * 10_000
        held = max(0, index - int(meta.get("entry_bar", index)))
        execution_cost_bps = self.costs.round_trip_bps(held)
        funding_cost_usd_value = float(meta.get("funding_cost_usd") or 0.0)
        notional = float(meta.get("notional_usd", self.notional_usd))
        funding_bps = (
            funding_cost_usd_value / notional * 10_000 if notional > 0 else 0.0
        )
        total_cost_bps = execution_cost_bps + funding_bps
        net_bps = gross_bps - total_cost_bps
        net_usd = net_bps * notional / 10_000
        gross_usd = gross_bps * notional / 10_000
        profile = (
            self.costs.cost_model.profile
            if self.costs.cost_model is not None
            else "legacy_fee_only"
        )
        entry_ts = meta.get("entry_ts") or meta.get("bar_ts")
        entry_ts_text = entry_ts.isoformat() if isinstance(entry_ts, datetime) else str(entry_ts)
        mfe_bps = decision.mfe / entry * 10_000
        mae_bps = decision.mae / entry * 10_000
        self.outcomes += 1
        self.net_usd += net_usd
        self.journal.append(
            "shadow_outcome",
            {
                "intent_key": meta.get("intent_key"),
                "decision_id": meta.get("intent_key"),
                "path_id": KERNEL_PATH_ID,
                "strategy_id": self.strategy_id,
                "symbol": self.symbol,
                "resolution": decision.reason,
                "side": side,
                "entry_price": entry,
                "exit_price": decision.price,
                "bars_held": held,
                "virtual_net_usd": net_usd,
                "gross_pnl_usd": gross_usd,
                "fees_usd": execution_cost_bps * notional / 10_000,
                "funding_usd": funding_cost_usd_value,
                "funding_events": len(meta.get("funding_event_ids") or ()),
                "funding_bps": funding_bps,
                "funding_complete": True,
                "notional_usd": notional,
                "margin_usd": float(meta.get("margin_usd", self.margin_usd)),
                "captured_bps": gross_bps,
                "net_bps": net_bps,
                "execution_cost_bps": execution_cost_bps,
                "round_trip_cost_bps": total_cost_bps,
                "cost_profile": profile,
                "cost_contract_version": "scanner_cost_v1",
                "build_sha": os.environ.get("VNEDGE_BUILD_SHA", "unknown"),
                "entry_ts": entry_ts_text,
                "exit_ts": bar_ts.isoformat(),
                "mfe_price_delta": decision.mfe,
                "mae_price_delta": decision.mae,
                "mfe_bps": mfe_bps,
                "mae_bps": mae_bps,
                "captured_bps_basis": "gross",
                "net_won": net_bps > 0,
                "signal_reason": meta.get("reason", ""),
                "arm_evidence": meta.get("arm_evidence"),
                "arm_envelope": meta.get("arm_envelope"),
                "execution_evidence": meta.get("execution_evidence"),
                "bar_ts": bar_ts.isoformat(),
            },
        )
        return net_bps > 0

    @property
    def has_open(self) -> bool:
        return self.open_meta is not None

    def stats(self) -> dict:
        open_position = self._open_position_stats()
        open_net_usd = (
            float(open_position["unrealized_net_usd"]) if open_position is not None else 0.0
        )
        return {
            "virtual_trades": self.outcomes,
            # Compatibility: net_usd remains CLOSED/resolved PnL. Consumers
            # that want current shadow equity must use total_net_usd.
            "net_usd": round(self.net_usd, 4),
            "virtual_net_usd": round(self.net_usd, 4),
            "resolved_net_usd": round(self.net_usd, 4),
            "open_unrealized_net_usd": round(open_net_usd, 4),
            "total_net_usd": round(self.net_usd + open_net_usd, 4),
            "open_position": open_position,
            "open_intents": int(self.has_open),
            "pending_shadow_intents": int(self.has_open),
            "candidates": self.candidates,
            "armed_entries": len(self._armed_episodes),
            "approved": self.fires,
            "rejected": self.rejected,
            "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
            "conversion_rejections": dict(sorted(self.conversion_rejections.items())),
            "cost_rejected": self.rejection_reasons.get("cost", 0),
            "sizing_rejected": self.rejection_reasons.get("sizing", 0),
            "risk_rejected": self.rejection_reasons.get("risk", 0),
            "portfolio_rejected": self.rejection_reasons.get("portfolio", 0),
            "prerequisite_rejected": self.rejection_reasons.get("prerequisite", 0),
            "acceptance_state": self.acceptance.last_reason,
            "quote_source": self.acceptance.last_quote_source,
            "quote_ingest_lag_seconds": round(self.acceptance.last_quote_lag_seconds, 6),
            "quotes_seen": self.acceptance.quotes_seen,
            "quotes_distinct": self.acceptance.quotes_distinct,
            "quote_contract_rejects": self.acceptance.quote_contract_rejects,
            "book_filter_rejects": self.acceptance.book_filter_rejects,
            "book_imbalance": self.acceptance.last_book_imbalance,
            "book_spread_ticks": self.acceptance.last_book_spread_ticks,
            "quote_overflow_drops": self.acceptance.quote_overflow_drops,
            "quote_rearms": self.acceptance.quote_rearms,
            "overflow_probe_resets": self.acceptance.overflow_probe_resets,
        }

    def _open_position_stats(self) -> dict | None:
        """Mark the virtual position to the last executable quote.

        Longs exit at bid and shorts at ask. The estimate includes the full
        booked round-trip profile, so it cannot present a gross winner as net
        profit. It is display-only and never feeds sizing or exits.
        """
        meta = self.open_meta
        pos = self.exits.pos
        if meta is None or pos is None or self._last_bid is None or self._last_ask is None:
            return None
        side = str(meta.get("side") or pos.side)
        entry = float(meta.get("entry") or pos.entry)
        mark = self._last_bid if side == "long" else self._last_ask
        if not (math.isfinite(entry) and math.isfinite(mark) and entry > 0 and mark > 0):
            return None
        gross_bps = ((mark / entry - 1) if side == "long" else (1 - mark / entry)) * 10_000
        held = max(0, self.current_bar_index - int(meta.get("entry_bar", self.current_bar_index)))
        cost_bps = self.costs.round_trip_bps(held)
        net_bps = gross_bps - cost_bps
        notional = float(meta.get("notional_usd", self.notional_usd))
        return {
            "side": side,
            "entry_price": round(entry, 10),
            "mark_price": round(mark, 10),
            "mark_basis": "executable_bid" if side == "long" else "executable_ask",
            "bars_held": held,
            "notional_usd": round(notional, 4),
            "margin_usd": round(float(meta.get("margin_usd", self.margin_usd)), 4),
            "unrealized_gross_bps": round(gross_bps, 4),
            "estimated_round_trip_cost_bps": round(cost_bps, 4),
            "unrealized_net_bps": round(net_bps, 4),
            "unrealized_gross_usd": round(gross_bps * notional / 10_000, 4),
            "unrealized_net_usd": round(net_bps * notional / 10_000, 4),
        }
