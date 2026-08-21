"""Squeeze observer runner: the trigger/exit-plane lane for shadow observe.

Routes ``squeeze_expansion_breakout_v2`` through the shared TriggerEngine and
ExitEngine instead of the generic SignalIntent + fixed-TP path, so the VM
shadow journal records the same plane the research replay measures.  Journals
the standard ``shadow_intent`` / ``shadow_outcome`` records; emits no orders.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast

import pandas as pd

from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.execution.trigger_engine import (
    ArmState,
    FireDecision,
    Side,
    TriggerConfig,
    TriggerEngine,
)
from vnedge.runtime.scanner_session import SessionCosts


class JournalSink(Protocol):
    def append(self, kind: str, payload: dict[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class ScannerApproval:
    """Risk/sizing verdict supplied by the owning shadow runtime."""

    approved: bool
    intent: dict
    failed_checks: tuple[str, ...] = ()
    passed_checks: tuple[str, ...] = ()
    explanation: str = ""
    notional_usd: float = 0.0
    margin_usd: float = 0.0


FireGuard = Callable[[FireDecision, int, datetime], ScannerApproval]


@dataclass
class SqueezeObserveRunner:
    """Per-lane engine driver over the strategy's prepared columns."""

    journal: JournalSink
    symbol: str
    notional_usd: float = 3000.0
    margin_usd: float = 100.0
    approve_fire: FireGuard | None = None
    trigger: TriggerEngine = field(
        default_factory=lambda: TriggerEngine(config=TriggerConfig())
    )
    exits: ExitEngine = field(default_factory=lambda: ExitEngine(config=ExitConfig()))
    costs: SessionCosts = field(
        default_factory=lambda: SessionCosts.from_profile("delta_scalp")
    )
    open_meta: dict | None = None
    fires: int = 0
    outcomes: int = 0
    rejected: int = 0
    candidates: int = 0
    net_usd: float = 0.0
    last_approval: ScannerApproval | None = None
    _restore_payload: dict | None = field(default=None, init=False, repr=False)
    _restore_error: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Locate an unresolved durable intent without creating a new one."""
        read_all = getattr(self.journal, "read_all", None)
        if not callable(read_all):
            return
        scanner_intents: dict[str, dict] = {}
        intents: dict[str, dict] = {}
        resolved: set[str] = set()
        for record in read_all():
            kind = record.get("kind")
            payload = record.get("payload", {})
            key = str(payload.get("intent_key") or "")
            if kind == "shadow_intent" and key.startswith("squeeze_observe|"):
                scanner_intents[key] = payload
                if payload.get("approved"):
                    intents[key] = payload
            elif kind == "shadow_outcome" and key:
                resolved.add(key)
                if key.startswith("squeeze_observe|"):
                    self.outcomes += 1
                    self.net_usd += float(payload.get("virtual_net_usd") or 0.0)
        self.candidates = len(scanner_intents)
        self.fires = len(intents)
        self.rejected = sum(
            not bool(payload.get("approved"))
            for payload in scanner_intents.values()
        )
        pending = [payload for key, payload in intents.items() if key not in resolved]
        if len(pending) > 1:
            self._restore_error = (
                f"{len(pending)} unresolved scanner intents; single-book invariant violated"
            )
        elif pending:
            self._restore_payload = pending[0]

    def restore(self, df: pd.DataFrame) -> None:
        """Rebuild one open scanner position and replay only its missing bars.

        A restart must neither drop an open virtual trade nor create a fresh
        decision from seed history. The durable intent carries the complete
        trigger geometry; seeded canonical bars advance that same ExitEngine
        until the trade resolves or reaches the live boundary.
        """
        payload = self._restore_payload
        if payload is None or self._restore_error is not None:
            return
        required = ("entry_price", "stop_price", "box_edge", "risk", "episode_id", "bar_ts")
        missing = [name for name in required if payload.get(name) is None]
        if missing or "timestamp" not in df.columns:
            self._restore_error = f"scanner restore missing {missing or ['timestamp']}"
            return
        timestamps = pd.to_datetime(df["timestamp"], utc=True)
        decision_ts = pd.Timestamp(payload["bar_ts"])
        matches = [i for i, ts in enumerate(timestamps) if ts == decision_ts]
        if not matches:
            self._restore_error = "scanner decision bar absent from canonical warmup"
            return
        entry_index = matches[-1]
        intent = payload.get("intent") or {}
        side = str(intent.get("side") or "")
        if side not in {"long", "short"}:
            self._restore_error = "scanner restore has invalid side"
            return
        self.exits.open_from_fire(
            side=cast(Side, side),
            entry=float(payload["entry_price"]),
            stop=float(payload["stop_price"]),
            risk=float(payload["risk"]),
            box_edge=float(payload["box_edge"]),
            entry_bar=entry_index,
        )
        self.trigger.position_open = True
        self.trigger.fired_episode = int(payload["episode_id"])
        self.trigger.last_fire_bar = entry_index
        self.open_meta = {
            "side": side,
            "entry": float(payload["entry_price"]),
            "entry_bar": entry_index,
            "intent_key": payload.get("intent_key"),
            "reason": payload.get("signal_reason", ""),
            "bar_ts": decision_ts.to_pydatetime(),
            "notional_usd": float(intent.get("notional_usd") or self.notional_usd),
            "margin_usd": float(payload.get("margin_usd") or self.margin_usd),
        }
        self._restore_payload = None
        for index in range(entry_index + 1, len(df)):
            self.on_prepared_bar(df, index, timestamps.iloc[index].to_pydatetime())
            if self.open_meta is None:
                break

    def on_prepared_bar(
        self, df: pd.DataFrame, index: int, bar_ts: datetime
    ) -> FireDecision | None:
        """Called once per closed 5m bar with the strategy's prepared frame."""
        row = df.iloc[index]
        needed = (
            "sqz_range_high", "sqz_range_low", "sqz_compressed", "sqz_episode",
            "sqz_atr", "sqz_vol_ma", "sqz_vwap24", "high", "low", "close", "volume",
        )
        values = {}
        for name in needed:
            value = float(row[name]) if name in row else float("nan")
            values[name] = value
        if any(not math.isfinite(values[n]) for n in ("high", "low", "close")):
            return None
        if self._restore_error is not None:
            return None
        atr = values["sqz_atr"]
        vwap = values["sqz_vwap24"]

        if self.open_meta is not None:
            decision = self.exits.on_bar(
                high=values["high"], low=values["low"], close=values["close"],
                atr=atr if math.isfinite(atr) else 0.0, bar_index=index,
            )
            if decision is not None:
                self._journal_outcome(decision, index, bar_ts)
                self.trigger.notify_flat(index, won=decision.won)
                self.open_meta = None
            return None

        if index <= 0 or not math.isfinite(atr) or not math.isfinite(vwap):
            return None
        prev_close = float(df.iloc[index - 1]["close"])
        fire = self.trigger.try_fire(
            arm=ArmState(
                episode_id=int(values["sqz_episode"]),
                box_high=values["sqz_range_high"],
                box_low=values["sqz_range_low"],
                compressed=values["sqz_compressed"] > 0,
                atr=atr,
                vol_ma=values["sqz_vol_ma"],
                prev_close=prev_close,
            ),
            high=values["high"], low=values["low"], close=values["close"],
            volume=values["volume"], vwap=vwap,
            bar_index=index, bar_ts_ms=int(bar_ts.timestamp() * 1000),
        )
        if fire is None:
            return None
        self.candidates += 1
        approval = (
            self.approve_fire(fire, index, bar_ts)
            if self.approve_fire is not None
            else ScannerApproval(
                approved=True,
                intent={
                    "symbol": self.symbol,
                    "side": fire.side,
                    "notional_usd": self.notional_usd,
                    "strategy_id": "squeeze_expansion_breakout_v2",
                    "order_type": "stop_through",
                },
                passed_checks=("trigger_engine",),
                explanation=fire.reason,
                notional_usd=self.notional_usd,
                margin_usd=self.margin_usd,
            )
        )
        self.last_approval = approval
        key = f"squeeze_observe|{self.symbol}|{fire.side}|{int(bar_ts.timestamp() * 1000)}"
        self.journal.append("shadow_intent", {
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
            "episode_id": fire.episode_id,
            "margin_usd": approval.margin_usd or self.margin_usd,
            "take_profit_price": None,
            "take_profit_levels": [],
            "bar_ts": bar_ts.isoformat(),
        })
        if not approval.approved:
            self.rejected += 1
            # The episode remains burned, but there is no position lock after
            # a central risk rejection.
            self.trigger.notify_cancelled(index)
            return fire
        self.exits.open_from_fire(
            side=fire.side, entry=fire.entry, stop=fire.stop, risk=fire.risk,
            box_edge=fire.box_edge, entry_bar=index,
        )
        self.open_meta = {
            "side": fire.side, "entry": fire.entry, "entry_bar": index,
            "intent_key": key, "reason": fire.reason, "bar_ts": bar_ts,
            "notional_usd": approval.notional_usd or self.notional_usd,
            "margin_usd": approval.margin_usd or self.margin_usd,
        }
        self.fires += 1
        return fire

    def _journal_outcome(self, decision, index: int, bar_ts: datetime) -> None:
        meta = self.open_meta or {}
        side = meta.get("side", "long")
        entry = float(meta.get("entry", 0.0)) or 1.0
        held = index - int(meta.get("entry_bar", index))
        gross_bps = (
            (decision.price / entry - 1) if side == "long" else (1 - decision.price / entry)
        ) * 1e4
        fee_bps = self.costs.round_trip_bps(held)
        net_bps = gross_bps - fee_bps
        notional = float(meta.get("notional_usd", self.notional_usd))
        margin = float(meta.get("margin_usd", self.margin_usd))
        net_usd = net_bps * notional / 1e4
        self.outcomes += 1
        self.net_usd += net_usd
        entry_bar_ts = meta.get("bar_ts")
        self.journal.append("shadow_outcome", {
            "intent_key": meta.get("intent_key"),
            "resolution": decision.reason,
            "side": side,
            # entry bar is carried on the outcome so a reader can reconcile
            # arm -> fire -> exit without re-joining against the intent record
            "entry_bar_ts": entry_bar_ts.isoformat() if entry_bar_ts else None,
            "entry_price": entry,
            "exit_price": decision.price,
            "bars_held": held,
            "virtual_net_usd": net_usd,
            "fees_usd": fee_bps * notional / 1e4,
            "notional_usd": notional,
            "margin_usd": margin,
            "captured_bps": gross_bps,
            "captured_bps_basis": "gross",
            "signal_reason": meta.get("reason", ""),
            "bar_ts": bar_ts.isoformat(),
        })

    @property
    def has_open(self) -> bool:
        return self.open_meta is not None

    def stats(self) -> dict:
        return {
            "virtual_trades": self.outcomes,
            "net_usd": round(self.net_usd, 4),
            "virtual_net_usd": round(self.net_usd, 4),
            "open_intents": int(self.has_open),
            "pending_shadow_intents": int(self.has_open),
            "candidates": self.candidates,
            "approved": self.fires,
            "rejected": self.rejected,
            "status": "RESTORE_BLOCKED" if self._restore_error else "OBSERVE",
            "trade_compatible": self._restore_error is None,
            "exit_semantics": "canonical_scanner_exit_engine",
            "route": "taker",
            "restore_error": self._restore_error,
        }
