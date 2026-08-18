"""Squeeze observer runner: the trigger/exit-plane lane for shadow observe.

Routes ``squeeze_expansion_breakout_v2`` through the shared TriggerEngine and
ExitEngine instead of the generic SignalIntent + fixed-TP path, so the VM
shadow journal records the same plane the research replay measures.  Journals
the standard ``shadow_intent`` / ``shadow_outcome`` records; emits no orders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.execution.trigger_engine import ArmState, TriggerConfig, TriggerEngine

TAKER_BPS = 5.9
SCALPER_FREE_CLOSE_BARS = 6


@dataclass
class SqueezeObserveRunner:
    """Per-lane engine driver over the strategy's prepared columns."""

    journal: object  # DecisionJournal-compatible (append(kind, payload))
    symbol: str
    notional_usd: float = 3000.0
    margin_usd: float = 100.0
    trigger: TriggerEngine = field(
        default_factory=lambda: TriggerEngine(config=TriggerConfig())
    )
    exits: ExitEngine = field(default_factory=lambda: ExitEngine(config=ExitConfig()))
    open_meta: dict | None = None
    fires: int = 0
    outcomes: int = 0

    def on_prepared_bar(self, df: pd.DataFrame, index: int, bar_ts: datetime) -> None:
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
            return
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
            return

        if index <= 0 or not math.isfinite(atr) or not math.isfinite(vwap):
            return
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
            return
        self.exits.open_from_fire(
            side=fire.side, entry=fire.entry, stop=fire.stop, risk=fire.risk,
            box_edge=fire.box_edge, entry_bar=index,
        )
        key = f"squeeze_observe|{self.symbol}|{fire.side}|{int(bar_ts.timestamp() * 1000)}"
        self.open_meta = {
            "side": fire.side, "entry": fire.entry, "entry_bar": index,
            "intent_key": key, "reason": fire.reason, "bar_ts": bar_ts,
        }
        self.fires += 1
        self.journal.append("shadow_intent", {
            "intent_key": key, "approved": True, "failed_checks": [],
            "passed_checks": ["trigger_engine"], "explanation": fire.reason,
            "intent": {
                "symbol": self.symbol, "side": fire.side,
                "notional_usd": self.notional_usd, "strategy_id":
                "squeeze_expansion_breakout_v2", "order_type": "stop_through",
            },
            "signal_reason": fire.reason, "stop_price": fire.stop,
            "take_profit_price": None, "take_profit_levels": [],
            "bar_ts": bar_ts.isoformat(),
        })

    def _journal_outcome(self, decision, index: int, bar_ts: datetime) -> None:
        meta = self.open_meta or {}
        side = meta.get("side", "long")
        entry = float(meta.get("entry", 0.0)) or 1.0
        held = index - int(meta.get("entry_bar", index))
        gross_bps = (
            (decision.price / entry - 1) if side == "long" else (1 - decision.price / entry)
        ) * 1e4
        fee_bps = TAKER_BPS + (0.0 if held <= SCALPER_FREE_CLOSE_BARS else TAKER_BPS)
        net_bps = gross_bps - fee_bps
        self.outcomes += 1
        self.journal.append("shadow_outcome", {
            "intent_key": meta.get("intent_key"),
            "resolution": decision.reason,
            "side": side,
            "entry_price": entry,
            "exit_price": decision.price,
            "bars_held": held,
            "virtual_net_usd": net_bps * self.notional_usd / 1e4,
            "fees_usd": fee_bps * self.notional_usd / 1e4,
            "notional_usd": self.notional_usd,
            "margin_usd": self.margin_usd,
            "captured_bps": gross_bps,
            "captured_bps_basis": "gross",
            "signal_reason": meta.get("reason", ""),
            "bar_ts": bar_ts.isoformat(),
        })
