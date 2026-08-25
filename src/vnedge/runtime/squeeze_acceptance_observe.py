"""Durable shadow runner for ``squeeze_expansion_breakout_v3``."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import cast

import pandas as pd

from vnedge.execution.exit_engine import ExitConfig, ExitDecision, ExitEngine
from vnedge.execution.trigger_engine import FireDecision, Side
from vnedge.runtime.expansion_acceptance import CompressionArm, ExpansionAcceptanceEngine
from vnedge.runtime.scanner_session import SessionCosts
from vnedge.runtime.squeeze_observe import FireGuard, JournalSink, ScannerApproval
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
    open_meta: dict | None = None
    candidates: int = 0
    fires: int = 0
    rejected: int = 0
    outcomes: int = 0
    net_usd: float = 0.0
    current_bar_index: int = -1
    last_approval: ScannerApproval | None = None
    _restore_payload: dict | None = field(default=None, init=False, repr=False)
    _restore_error: str | None = field(default=None, init=False, repr=False)
    _last_journaled_acceptance: str | None = field(default=None, init=False, repr=False)
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
        for record in read_all():
            payload = record.get("payload", {})
            key = str(payload.get("intent_key") or "")
            if record.get("kind") == "shadow_intent" and key.startswith(f"{self.intent_prefix}|"):
                self.candidates += 1
                if payload.get("approved"):
                    intents[key] = payload
                    self.fires += 1
                else:
                    self.rejected += 1
            elif record.get("kind") == "shadow_outcome" and key.startswith(
                f"{self.intent_prefix}|"
            ):
                resolved.add(key)
                self.outcomes += 1
                self.net_usd += float(payload.get("virtual_net_usd") or 0.0)
        pending = [payload for key, payload in intents.items() if key not in resolved]
        if len(pending) > 1:
            self._restore_error = "multiple unresolved v3 scanner intents"
        elif pending:
            self._restore_payload = pending[0]

    def restore(self, df: pd.DataFrame) -> None:
        """Rebuild arm state and at most one durable virtual position."""
        self._prepared_frame = df
        timestamps = pd.to_datetime(df.get("timestamp"), utc=True)
        pending = self._restore_payload
        decision_index: int | None = None
        if pending is not None:
            decision_ts = pd.Timestamp(pending.get("bar_ts"))
            # V3 decisions are quote-timestamped, not candle-open stamped.
            # Reattach them to the latest causal closed-bar row at or before
            # the quote; never use a future candle during restart recovery.
            matches = [i for i, ts in enumerate(timestamps) if ts <= decision_ts]
            if not matches:
                self._restore_error = "v3 scanner decision bar absent from warmup"
                return
            decision_index = matches[-1]

        for index in range(len(df)):
            self.current_bar_index = index
            self._update_arm_from_row(df.iloc[index], index)
            if pending is not None and index == decision_index:
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
                    "notional_usd": float(intent.get("notional_usd") or self.notional_usd),
                    "margin_usd": float(pending.get("margin_usd") or self.margin_usd),
                }
                self._restore_payload = None
                continue
            if self.open_meta is not None and decision_index is not None and index > decision_index:
                row = df.iloc[index]
                decision = self.exits.on_bar(
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    atr=self._row_atr(row),
                    bar_index=index,
                )
                decision = decision or self._strategy_exit(df, index, row)
                if decision is not None:
                    net_won = self._journal_outcome(
                        decision, index, timestamps.iloc[index].to_pydatetime()
                    )
                    self.acceptance.notify_flat(bar_index=index, net_won=net_won)
                    self.open_meta = None

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
            decision = self.exits.on_bar(
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                atr=self._row_atr(row),
                bar_index=index,
            )
            decision = decision or self._strategy_exit(df, index, row)
            if decision is not None:
                net_won = self._journal_outcome(decision, index, bar_ts)
                self.acceptance.notify_flat(bar_index=index, net_won=net_won)
                self.open_meta = None
        self._update_arm_from_row(row, index)

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
    ) -> FireDecision | None:
        if math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask:
            self._last_bid = bid
            self._last_ask = ask
        if self._restore_error is not None:
            return None
        self.acceptance.note_quote_overflow(overflow_drops)
        if self.open_meta is not None:
            pos = self.exits.pos
            price = bid if pos is not None and pos.side == "long" else ask
            decision = self.exits.on_tick(price=price)
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
        )
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
            )
        )
        self.last_approval = approval
        key = approval.intent_key or (
            f"{self.intent_prefix}|{self.symbol}|{fire.side}|{int(ts.timestamp() * 1000)}"
        )
        self.journal.append(
            "shadow_intent",
            {
                "intent_key": key,
                "approved": approval.approved,
                "failed_checks": list(approval.failed_checks),
                "passed_checks": list(approval.passed_checks),
                "explanation": approval.explanation or fire.reason,
                "intent": approval.intent,
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
                "margin_usd": approval.margin_usd or self.margin_usd,
                "take_profit_price": None,
                "take_profit_levels": [],
                "bar_ts": ts.isoformat(),
                "acceptance": "quote_hold",
            },
        )
        if not approval.approved:
            self.rejected += 1
            self.acceptance.notify_rejected()
            return fire
        self.exits.open_from_fire(
            side=fire.side,
            entry=fire.entry,
            stop=fire.stop,
            risk=fire.risk,
            box_edge=fire.box_edge,
            entry_bar=self.current_bar_index,
        )
        self.open_meta = {
            "side": fire.side,
            "entry": fire.entry,
            "entry_bar": self.current_bar_index,
            "intent_key": key,
            "reason": fire.reason,
            "bar_ts": ts,
            "notional_usd": approval.notional_usd or self.notional_usd,
            "margin_usd": approval.margin_usd or self.margin_usd,
        }
        self.fires += 1
        return fire

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
        if reason == self._last_journaled_acceptance:
            return
        self._last_journaled_acceptance = reason
        arm = self.acceptance.arm
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
                "quote_overflow_drops": self.acceptance.quote_overflow_drops,
                "quote_rearms": self.acceptance.quote_rearms,
                "overflow_probe_resets": self.acceptance.overflow_probe_resets,
            },
        )

    def _update_arm_from_row(self, row: pd.Series, index: int) -> None:
        if self.strategy is not None:
            # Give a concrete quote-entry strategy its complete causal frame.
            # ``_prepared_frame`` is attached by on_prepared_bar/restore below;
            # falling back to legacy squeeze columns preserves old revisions.
            prepared = getattr(self, "_prepared_frame", None)
            if isinstance(prepared, pd.DataFrame):
                arm = self.strategy.realtime_arm(prepared, index)
                if isinstance(arm, RealtimeEntryArm):
                    self.acceptance.update_arm(self._compression_arm(arm, index))
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
            )
        )

    @staticmethod
    def _compression_arm(arm: RealtimeEntryArm, index: int) -> CompressionArm:
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
        fee_bps = self.costs.round_trip_bps(held)
        net_bps = gross_bps - fee_bps
        notional = float(meta.get("notional_usd", self.notional_usd))
        net_usd = net_bps * notional / 10_000
        self.outcomes += 1
        self.net_usd += net_usd
        self.journal.append(
            "shadow_outcome",
            {
                "intent_key": meta.get("intent_key"),
                "resolution": decision.reason,
                "side": side,
                "entry_price": entry,
                "exit_price": decision.price,
                "bars_held": held,
                "virtual_net_usd": net_usd,
                "fees_usd": fee_bps * notional / 10_000,
                "notional_usd": notional,
                "margin_usd": float(meta.get("margin_usd", self.margin_usd)),
                "captured_bps": gross_bps,
                "net_bps": net_bps,
                "captured_bps_basis": "gross",
                "net_won": net_bps > 0,
                "signal_reason": meta.get("reason", ""),
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
            "approved": self.fires,
            "rejected": self.rejected,
            "acceptance_state": self.acceptance.last_reason,
            "quote_source": self.acceptance.last_quote_source,
            "quote_ingest_lag_seconds": round(self.acceptance.last_quote_lag_seconds, 6),
            "quotes_seen": self.acceptance.quotes_seen,
            "quotes_distinct": self.acceptance.quotes_distinct,
            "quote_contract_rejects": self.acceptance.quote_contract_rejects,
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
