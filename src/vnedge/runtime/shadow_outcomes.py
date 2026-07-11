"""Virtual outcome resolution for shadow lanes — per-lane edge visibility.

Shadow lanes journal approved entry intents (``shadow_intent``) but never
fill, so their realized PnL is structurally zero and the operator cannot
see WHICH lane has edge. This module resolves each approved shadow intent
forward on subsequent closed bars into a VIRTUAL trade with the same
conservative semantics the backtester enforces:

- entry at the recorded reference price (the intent's notional/quantity —
  the adverse book side captured at intent time, so the spread cost is
  already embedded in the entry);
- the first bar AFTER the decision bar is the virtual fill bar (decisions
  at close, fills at next open — same as research), and a stop can be hit
  in the fill bar itself;
- intrabar exits resolve stop-first on ties: if both stop and target lie
  inside one bar's range, the STOP fills (``backtester._check_intrabar_exit``
  convention);
- max-holding timeout closes at bar close after ``max_holding_bars`` bars,
  mirroring ``run_backtest``'s ``j - entry_bar >= max_holding_bars``;
- taker fees on BOTH virtual fills via the paper ``FillModel`` — if virtual
  results look better than paper fills would have been, the model is wrong.

Each resolution is journaled as a ``shadow_outcome`` record. The journal
itself is the durable store: on restart the tracker reloads unresolved
intents and skips every intent_key that already has an outcome record, so
nothing is ever resolved twice.

Virtual outcomes are OBSERVABILITY ONLY: they gate nothing, promote
nothing, and trade nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from vnedge.execution.journal import DecisionJournal
from vnedge.paper.fill_model import FillModel

logger = logging.getLogger(__name__)


@dataclass
class _PendingIntent:
    intent_key: str
    side: str  # "long" | "short"
    quantity: float
    notional_usd: float
    entry_price: float  # recorded ref quote = notional / quantity
    stop_price: float
    take_profit_price: float | None
    decision_bar_ts: pd.Timestamp  # bar whose close produced the intent
    signal_reason: str = ""
    bars_held: int = -1  # -1 = virtual fill not reached yet; fill bar = 0


@dataclass(frozen=True)
class VirtualOutcome:
    intent_key: str
    resolution: str  # "stop" | "target" | "timeout"
    bars_held: int
    virtual_net_usd: float
    side: str
    entry_price: float
    exit_price: float
    fees_usd: float
    resolved_bar_ts: str


class ShadowOutcomeTracker:
    """Resolves a shadow lane's journaled intents into virtual outcomes.

    Constructed from the lane's own decision journal: approved
    ``shadow_intent`` records without a matching ``shadow_outcome`` record
    become the pending set; already-resolved outcomes seed the cumulative
    stats so restarts keep the lane's virtual track record intact.
    """

    def __init__(
        self,
        journal: DecisionJournal,
        *,
        fill_model: FillModel | None = None,
        max_holding_bars: int = 48,
    ) -> None:
        self.journal = journal
        self.fill_model = fill_model or FillModel()
        self.max_holding_bars = max_holding_bars
        self._pending: dict[str, _PendingIntent] = {}
        self._resolved_keys: set[str] = set()
        self._trades = 0
        self._wins = 0
        self._net_usd = 0.0
        self._gross_win_usd = 0.0
        self._gross_loss_usd = 0.0
        self._resolutions: dict[str, int] = {"stop": 0, "target": 0, "timeout": 0}
        self._load()

    # --- journal replay ----------------------------------------------------------
    def _load(self) -> None:
        intents: dict[str, dict] = {}
        for record in self.journal.read_all():
            kind, payload = record.get("kind"), record.get("payload", {})
            if kind == "shadow_intent":
                key = payload.get("intent_key")
                if key and payload.get("approved") and key not in intents:
                    intents[key] = payload
            elif kind == "shadow_outcome":
                key = payload.get("intent_key")
                if key is None or key in self._resolved_keys:
                    continue
                self._resolved_keys.add(key)
                self._accumulate(
                    str(payload.get("resolution", "")),
                    float(payload.get("virtual_net_usd", 0.0)),
                )
        for key, payload in intents.items():
            if key in self._resolved_keys:
                continue
            pending = self._parse_intent(key, payload)
            if pending is not None:
                self._pending[key] = pending

    @staticmethod
    def _parse_intent(key: str, payload: dict) -> _PendingIntent | None:
        """Build a pending virtual position from a journaled shadow_intent.

        Records predating outcome tracking lack stop/target/bar_ts and are
        skipped — they cannot be resolved honestly."""
        intent = payload.get("intent") or {}
        stop = payload.get("stop_price")
        bar_ts = payload.get("bar_ts")
        quantity = float(intent.get("quantity") or 0.0)
        notional = float(intent.get("notional_usd") or 0.0)
        if stop is None or bar_ts is None or quantity <= 0 or notional <= 0:
            return None
        tp = payload.get("take_profit_price")
        return _PendingIntent(
            intent_key=key,
            side=str(intent.get("side", "long")),
            quantity=quantity,
            notional_usd=notional,
            entry_price=notional / quantity,
            stop_price=float(stop),
            take_profit_price=float(tp) if tp is not None else None,
            decision_bar_ts=pd.Timestamp(bar_ts),
            signal_reason=str(payload.get("signal_reason") or ""),
        )

    # --- live registration -------------------------------------------------------
    def track(
        self,
        *,
        intent_key: str,
        side: str,
        quantity: float,
        notional_usd: float,
        stop_price: float,
        take_profit_price: float | None,
        decision_bar_ts: pd.Timestamp,
        signal_reason: str = "",
    ) -> None:
        """Register a just-approved shadow intent for forward resolution."""
        if intent_key in self._pending or intent_key in self._resolved_keys:
            return  # restart re-prime can re-journal the same decision
        if quantity <= 0 or notional_usd <= 0:
            return
        self._pending[intent_key] = _PendingIntent(
            intent_key=intent_key,
            side=side,
            quantity=quantity,
            notional_usd=notional_usd,
            entry_price=notional_usd / quantity,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            decision_bar_ts=pd.Timestamp(decision_bar_ts),
            signal_reason=signal_reason,
        )

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    # --- resolution --------------------------------------------------------------
    def resolve_bar(self, bar: pd.Series) -> list[VirtualOutcome]:
        """Advance every pending intent through one closed bar.

        Only bars strictly AFTER an intent's decision bar count (the first
        such bar is the virtual fill bar). Stop wins ties, target second,
        timeout at bar close once ``max_holding_bars`` is reached."""
        bar_ts = pd.Timestamp(bar["timestamp"])
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        outcomes: list[VirtualOutcome] = []
        for pending in list(self._pending.values()):
            if bar_ts <= pending.decision_bar_ts:
                continue
            pending.bars_held += 1  # fill bar counts as 0, like run_backtest
            resolution, exit_price = self._check_exit(pending, high, low, close)
            if resolution is None:
                continue
            outcomes.append(self._close(pending, resolution, exit_price, bar_ts))
        return outcomes

    def replay(self, candles: pd.DataFrame) -> list[VirtualOutcome]:
        """Resolve restored pending intents against already-seen candles —
        the seeded warmup history on restart. Live bars then continue
        naturally via resolve_bar."""
        if not self._pending or candles.empty:
            return []
        outcomes: list[VirtualOutcome] = []
        for i in range(len(candles)):
            outcomes.extend(self.resolve_bar(candles.iloc[i]))
            if not self._pending:
                break
        return outcomes

    def _check_exit(
        self, pending: _PendingIntent, high: float, low: float, close: float
    ) -> tuple[str | None, float]:
        tp = pending.take_profit_price
        if pending.side == "long":
            if low <= pending.stop_price:
                return "stop", pending.stop_price
            if tp is not None and high >= tp:
                return "target", tp
        else:
            if high >= pending.stop_price:
                return "stop", pending.stop_price
            if tp is not None and low <= tp:
                return "target", tp
        if pending.bars_held >= self.max_holding_bars:
            return "timeout", close
        return None, 0.0

    def _close(
        self,
        pending: _PendingIntent,
        resolution: str,
        exit_price: float,
        bar_ts: pd.Timestamp,
    ) -> VirtualOutcome:
        direction = 1.0 if pending.side == "long" else -1.0
        gross = direction * pending.quantity * (exit_price - pending.entry_price)
        fees = self.fill_model.fee_usd(pending.notional_usd) + self.fill_model.fee_usd(
            pending.quantity * exit_price
        )
        net = gross - fees
        outcome = VirtualOutcome(
            intent_key=pending.intent_key,
            resolution=resolution,
            bars_held=max(pending.bars_held, 0),
            virtual_net_usd=round(net, 6),
            side=pending.side,
            entry_price=round(pending.entry_price, 8),
            exit_price=round(exit_price, 8),
            fees_usd=round(fees, 6),
            resolved_bar_ts=bar_ts.isoformat(),
        )
        del self._pending[pending.intent_key]
        self._resolved_keys.add(pending.intent_key)
        self._accumulate(resolution, net)
        self.journal.append("shadow_outcome", {
            "intent_key": outcome.intent_key,
            "resolution": outcome.resolution,
            "bars_held": outcome.bars_held,
            "virtual_net_usd": outcome.virtual_net_usd,
            "side": outcome.side,
            "entry_price": outcome.entry_price,
            "exit_price": outcome.exit_price,
            "fees_usd": outcome.fees_usd,
            "bar_ts": outcome.resolved_bar_ts,
            "signal_reason": pending.signal_reason,
        })
        logger.info(
            "shadow outcome: %s %s -> %s after %d bars, virtual %+0.2f USD",
            pending.side, pending.intent_key, resolution,
            outcome.bars_held, net,
        )
        return outcome

    def _accumulate(self, resolution: str, net: float) -> None:
        self._trades += 1
        self._net_usd += net
        if net > 0:
            self._wins += 1
            self._gross_win_usd += net
        else:
            self._gross_loss_usd += -net
        if resolution in self._resolutions:
            self._resolutions[resolution] += 1

    # --- reporting ----------------------------------------------------------------
    def stats(self) -> dict:
        """Per-lane virtual performance for session_stats / the dashboard."""
        if self._gross_loss_usd > 0:
            pf = round(self._gross_win_usd / self._gross_loss_usd, 3)
        else:
            pf = None  # no losing virtual trades yet — PF undefined, not infinite
        status = "SHADOW_PROBATION" if self._trades > 0 and self._net_usd < 0 else "OBSERVE"
        return {
            "virtual_trades": self._trades,
            "wins": self._wins,
            "losses": self._trades - self._wins,
            "net_usd": round(self._net_usd, 4),
            "profit_factor": pf,
            "open_intents": len(self._pending),
            "resolutions": dict(self._resolutions),
            "status": status,
            "trade_compatible": status != "SHADOW_PROBATION",
        }
