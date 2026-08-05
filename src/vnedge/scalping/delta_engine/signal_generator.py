"""Candidate gating, ranking, journaling, and risk-gateway adaptation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from vnedge.execution.journal import DecisionJournal
from vnedge.risk.risk_manager import AccountState, OrderIntent
from vnedge.scalping.delta_engine.context import MarketContextBuilder
from vnedge.scalping.delta_engine.scanners import Scanner
from vnedge.scalping.delta_engine.types import SignalCandidate
from vnedge.scalping.microstructure import MarketMicroState
from vnedge.scalping.risk import ScalperRiskDecision, ScalperRiskGateway


@dataclass(frozen=True)
class SignalGateConfig:
    min_expectancy_bps: float = 8.0
    min_probability: float = 0.70
    min_confidence: float = 0.60
    allowed_symbols: tuple[str, ...] = ("BTCUSD", "ETHUSD")
    primary_timeframes: tuple[str, ...] = ("1m", "5m")


@dataclass(frozen=True)
class EngineDecision:
    symbol: str
    decision_ts: datetime
    selected: SignalCandidate | None
    evaluated: tuple[SignalCandidate, ...]
    rejection_reasons: tuple[str, ...]
    duplicate: bool = False
    research_only: bool = True
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "decision_ts": self.decision_ts.isoformat(),
            "selected": self.selected.to_dict() if self.selected else None,
            "evaluated": [row.to_dict() for row in self.evaluated],
            "rejection_reasons": list(self.rejection_reasons),
            "duplicate": self.duplicate,
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
        }


class DeltaScalperSignalGenerator:
    """Builds and journals decisions but owns no order-submission capability."""

    def __init__(
        self,
        context_builder: MarketContextBuilder,
        scanners: tuple[Scanner, ...],
        *,
        journal: DecisionJournal | None = None,
        gates: SignalGateConfig | None = None,
    ) -> None:
        self.context_builder = context_builder
        self.scanners = scanners
        self.journal = journal
        self.gates = gates or SignalGateConfig()
        self._seen: set[str] = set()

    def on_candle_closed(
        self,
        symbol: str,
        timeframe: str,
        *,
        now: datetime | None = None,
    ) -> EngineDecision:
        if timeframe not in self.gates.primary_timeframes:
            current = now or datetime.now(UTC)
            return EngineDecision(symbol.upper(), current, None, (), ("non_primary_timeframe",))
        ctx = self.context_builder.build(symbol, now=now)
        candidates = tuple(
            candidate
            for scanner in self.scanners
            if (candidate := scanner.evaluate(ctx)) is not None
        )
        accepted: list[SignalCandidate] = []
        reasons: list[str] = []
        for candidate in candidates:
            failed: list[str] = []
            if candidate.symbol not in self.gates.allowed_symbols:
                failed.append("symbol_not_enabled")
            if candidate.fee_adjusted_expectancy_bps < self.gates.min_expectancy_bps:
                failed.append("fee_adjusted_expectancy_below_gate")
            if candidate.scalper_probability < self.gates.min_probability:
                failed.append("probability_below_gate")
            if candidate.confidence < self.gates.min_confidence:
                failed.append("confidence_below_gate")
            if candidate.expected_hold_seconds > candidate.time_stop_seconds:
                failed.append("scalper_window_noncompliant")
            if failed:
                reasons.extend(f"{candidate.scanner_id}:{reason}" for reason in failed)
            else:
                accepted.append(candidate)
        selected = max(accepted, key=lambda row: row.rank_score) if accepted else None
        duplicate = False
        if selected is not None:
            duplicate = selected.dedup_key in self._seen
            if duplicate:
                reasons.append(f"{selected.scanner_id}:duplicate_decision")
                selected = None
            else:
                self._seen.add(selected.dedup_key)
        decision = EngineDecision(
            symbol=ctx.symbol,
            decision_ts=ctx.ts,
            selected=selected,
            evaluated=candidates,
            rejection_reasons=tuple(reasons),
            duplicate=duplicate,
        )
        if self.journal is not None:
            self.journal.append("delta_scalper_research_decision", decision.to_dict())
        return decision


@dataclass(frozen=True)
class GatewayEvaluation:
    intent: OrderIntent
    risk: ScalperRiskDecision
    candidate: SignalCandidate
    submitted: bool = False


class ScalperRiskAdapter:
    """Converts a candidate into the existing gateway contract; never submits."""

    def __init__(self, gateway: ScalperRiskGateway, *, leverage: float = 1.0) -> None:
        self.gateway = gateway
        self.leverage = leverage

    def evaluate(
        self,
        candidate: SignalCandidate,
        *,
        notional_usd: float,
        account: AccountState,
        market: MarketMicroState,
        now: datetime | None = None,
    ) -> GatewayEvaluation:
        if notional_usd <= 0:
            raise ValueError("notional_usd must be positive")
        intent = OrderIntent(
            symbol=candidate.symbol,
            side=candidate.side.value,
            quantity=notional_usd / candidate.entry_price,
            notional_usd=notional_usd,
            leverage=self.leverage,
            reduce_only=False,
            strategy_id=candidate.scanner_id,
            order_type="limit" if candidate.entry_is_maker else "market",
            limit_price=candidate.entry_price if candidate.entry_is_maker else None,
            time_in_force="PO" if candidate.entry_is_maker else None,
        )
        risk = self.gateway.evaluate(
            intent,
            account,
            market,
            expected_edge_bps=candidate.expected_move_bps,
            now=now,
        )
        return GatewayEvaluation(intent=intent, risk=risk, candidate=candidate)
