"""HF SignalEngine contract + reference implementation.

The HF path's signal layer, distinct from the closed-candle
``strategy.base_strategy.SignalIntent`` (price-level, candle-driven). The HF
``SignalIntent`` here is bps-based and carries the two fields the CostGate needs —
``edge_estimate_bps`` and ``urgency`` — so a signal is cost-gated BEFORE it is sized.

Determinism: an engine is deterministic under ORDERED tick replay — the same tick
SEQUENCE reproduces the same intents (its only state is the last-signal timestamp,
which evolves monotonically). It never reads the future. ``signal_id`` is a
deterministic hash of the decision (symbol, ts, side), so idempotency holds and a
replayed tick dedupes to the SAME id (never a fresh random one).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Sequence

from pydantic import BaseModel, Field, field_validator

from vnedge.risk.cost_gate import CostGate, CostGateResult


# --- Core data contracts -----------------------------------------------------

class SignalIntent(BaseModel):
    """Immutable HF intent that flows into CostGate → Sizer → RiskGateway."""

    model_config = {"frozen": True}

    symbol: str
    side: str                               # "buy" | "sell"
    stop_distance_bps: Decimal
    take_profit_bps: Optional[Decimal] = None
    urgency: str                            # "maker" | "taker" | "aggressive"
    edge_estimate_bps: Decimal              # REQUIRED by the CostGate
    expected_holding_seconds: int
    signal_id: str                          # deterministic — idempotency + audit
    ts: datetime
    meta: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("side")
    @classmethod
    def _validate_side(cls, v: str) -> str:
        if v not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        return v

    @field_validator("urgency")
    @classmethod
    def _validate_urgency(cls, v: str) -> str:
        if v not in ("maker", "taker", "aggressive"):
            raise ValueError("urgency must be maker|taker|aggressive")
        return v


class TickSnapshot(BaseModel):
    """Minimal, fast snapshot the engine reads. Keep it small."""

    model_config = {"frozen": True}

    symbol: str
    ts: datetime
    last_price: Decimal
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    trade_imbalance_1s: Optional[Decimal] = None   # +1 buy-dominated, -1 sell-dominated
    volume_1s: Optional[Decimal] = None
    mid: Optional[Decimal] = None

    def model_post_init(self, __context: Any) -> None:
        if self.mid is None:  # computed default (frozen escape hatch)
            object.__setattr__(self, "mid", (self.bid + self.ask) / 2)


def make_signal_id(prefix: str, symbol: str, ts: datetime, side: str) -> str:
    """Deterministic id: same decision → same id (idempotency), never random."""
    src = f"{prefix}|{symbol}|{ts.isoformat()}|{side}"
    return f"{prefix}_{hashlib.sha256(src.encode()).hexdigest()[:12]}"


# --- Abstract base -----------------------------------------------------------

class SignalEngine(ABC):
    """Deterministic (under ordered replay) producer of HF SignalIntents."""

    engine_id: str = "signal_engine"

    @abstractmethod
    def generate(
        self,
        tick: TickSnapshot,
        account_equity: Decimal,
        open_positions: Sequence[Dict[str, Any]],
    ) -> Sequence[SignalIntent]:
        """Return 0..N intents. Empty is the normal case most ticks."""
        raise NotImplementedError


# --- Reference implementation: Order-Flow Imbalance Scalper ------------------

class OrderFlowImbalanceEngine(SignalEngine):
    """Reference HF microstructure engine — a SCAFFOLD, not a validated edge. Its
    ``edge_estimate_bps`` is a heuristic (imbalance strength − half-spread − safety);
    the CostGate then does the hard cost check, and promotion still requires the
    pre-registered ≥500-paper-trade discipline before any capital. Five locked knobs.
    """

    engine_id = "OrderFlowImbalanceEngine"

    def __init__(
        self,
        imbalance_threshold: Decimal = Decimal("0.35"),
        min_edge_bps: Decimal = Decimal("8.0"),
        stop_bps: Decimal = Decimal("12.0"),
        tp_bps: Decimal = Decimal("18.0"),
        max_holding_sec: int = 45,
        symbol: str = "BTCUSDT",
    ):
        self.imbalance_threshold = imbalance_threshold
        self.min_edge_bps = min_edge_bps
        self.stop_bps = stop_bps
        self.tp_bps = tp_bps
        self.max_holding_sec = max_holding_sec
        self.symbol = symbol
        self._last_signal_ts: Optional[datetime] = None   # cooldown state (ordered-replay det.)

    def generate(
        self,
        tick: TickSnapshot,
        account_equity: Decimal,
        open_positions: Sequence[Dict[str, Any]],
    ) -> Sequence[SignalIntent]:
        if tick.symbol != self.symbol or tick.trade_imbalance_1s is None:
            return []
        # flat-only v1: one symbol, one position
        if any(p.get("symbol") == self.symbol for p in open_positions):
            return []
        # short cooldown so 20-30/day stays achievable without over-firing on a burst
        if self._last_signal_ts and (tick.ts - self._last_signal_ts).total_seconds() < 3.0:
            return []

        imbalance = tick.trade_imbalance_1s
        spread_bps = ((tick.ask - tick.bid) / tick.mid) * Decimal("10000")

        side: Optional[str] = None
        edge = Decimal("0")
        if imbalance >= self.imbalance_threshold:
            side, edge = "buy", (imbalance * Decimal("15")) - (spread_bps / 2) - Decimal("2")
        elif imbalance <= -self.imbalance_threshold:
            side, edge = "sell", (abs(imbalance) * Decimal("15")) - (spread_bps / 2) - Decimal("2")

        if side is None or edge < self.min_edge_bps:
            return []

        # hybrid urgency: tight spread + moderate imbalance → post (maker); else take
        urgency = ("maker" if spread_bps <= Decimal("3.0") and abs(imbalance) < Decimal("0.55")
                   else "taker")

        intent = SignalIntent(
            symbol=tick.symbol, side=side,
            stop_distance_bps=self.stop_bps, take_profit_bps=self.tp_bps,
            urgency=urgency, edge_estimate_bps=edge.quantize(Decimal("0.01")),
            expected_holding_seconds=self.max_holding_sec,
            signal_id=make_signal_id("ofi", tick.symbol, tick.ts, side),
            ts=tick.ts,
            meta={"imbalance": str(imbalance), "spread_bps": str(spread_bps),
                  "engine": self.engine_id},
        )
        self._last_signal_ts = tick.ts
        return [intent]


def build_default_engines(symbols: Sequence[str] = ("BTCUSDT",)) -> list[SignalEngine]:
    """The production engine roster (currently one microstructure engine per symbol)."""
    return [OrderFlowImbalanceEngine(symbol=sym) for sym in symbols]


# --- Bridge: HF intent → CostGate (the hard filter, integration point #2) -----

def cost_gate_intent(
    intent: SignalIntent,
    gate: CostGate,
    current_funding_rate: object,
) -> CostGateResult:
    """Run one HF intent through the CostGate — the hard filter that lets us
    prove/kill the maker-HF economics on paper BEFORE building the loop."""
    return gate.evaluate(
        signal_edge_bps=intent.edge_estimate_bps,
        side=intent.side,
        urgency=intent.urgency,
        expected_holding_seconds=intent.expected_holding_seconds,
        current_funding_rate=current_funding_rate,
        symbol=intent.symbol,
    )
