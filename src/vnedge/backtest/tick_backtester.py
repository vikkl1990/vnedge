"""Minimal tick backtester — the HF economics validator.

Answers the ONE question that decides whether the HF restructure is worth
building: do any engine signals survive the CostGate with positive net bps after
realistic cost? Replays a tick/1s sequence → engines → CostGate → a PESSIMISTIC
fill/outcome model → a report (survival rate, net bps of survivors, trades/day).

Fill/outcome model (deliberately pessimistic, capital-protective):
- entry at the tick mid;
- stop fills at the stop level (never better), and STOP WINS TIES;
- take-profit fills at the TP level;
- time-stop exits at the current mid;
- realized net = signed gross move to the exit − the CostGate's full round-trip cost.

Single-symbol, single-position (matches the flat-only engines). Deterministic:
same tick sequence → same report.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

from pydantic import BaseModel

from vnedge.risk.cost_gate import CostGate
from vnedge.strategy.signal_engine import (
    SignalEngine,
    SignalIntent,
    TickSnapshot,
    cost_gate_intent,
)

_BPS = Decimal("10000")


@dataclass(frozen=True)
class _OpenTrade:
    intent: SignalIntent
    entry_price: Decimal
    entry_ts: datetime
    cost_bps: Decimal


class TickBacktestReport(BaseModel):
    model_config = {"frozen": True}

    ticks: int
    span_hours: float
    signals_generated: int
    cost_gate_survived: int
    survival_rate: float          # survived / generated
    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_net_bps: float            # over survivors (realized)
    median_net_bps: float
    total_net_bps: float
    avg_hold_seconds: float
    trades_per_day: float
    verdict: str                  # POSITIVE | MARGINAL | NEGATIVE | NO_TRADES


def _exit_net_bps(t: _OpenTrade, tick: TickSnapshot) -> Optional[Decimal]:
    """Return realized net bps if this tick closes the trade, else None."""
    signed = (tick.mid - t.entry_price) / t.entry_price * _BPS
    if t.intent.side == "sell":
        signed = -signed                                   # favorable-positive
    held = (tick.ts - t.entry_ts).total_seconds()
    stop = t.intent.stop_distance_bps
    tp = t.intent.take_profit_bps
    if signed <= -stop:                                    # stop wins ties
        gross = -stop
    elif tp is not None and signed >= tp:
        gross = tp
    elif held >= t.intent.expected_holding_seconds:
        gross = signed
    else:
        return None
    return gross - t.cost_bps


def run_tick_backtest(
    ticks: Sequence[TickSnapshot],
    engines: Sequence[SignalEngine],
    gate: CostGate,
    *,
    account_equity: Decimal = Decimal("500"),
    current_funding_rate: object = Decimal("0"),
    extra_cost_bps: object = Decimal("0"),
) -> TickBacktestReport:
    """``extra_cost_bps`` is subtracted from the REALIZED net of every trade — the
    honest slippage knob: the exit-sim gross move is unchanged, extra slippage just
    eats into it, so a sweep measures how much slippage the real edge tolerates
    (not the circular ``edge_estimate − cost``)."""
    signals, survived, nets, holds, span = _simulate(
        ticks, engines, gate, account_equity, current_funding_rate)
    return _report_from(len(ticks), signals, survived, nets, holds, span,
                        Decimal(str(extra_cost_bps)))


def _simulate(ticks, engines, gate, account_equity, current_funding_rate):
    """Run the pipeline ONCE and return the per-trade realized nets (at zero extra
    slippage) + hold times. Extra slippage only shifts nets uniformly and never
    changes which trades happen, so a sweep re-derives from this without re-running."""
    open_trade: Optional[_OpenTrade] = None
    signals = survived = 0
    nets: list[Decimal] = []
    holds: list[float] = []
    for tick in ticks:
        if open_trade is not None:
            net = _exit_net_bps(open_trade, tick)
            if net is not None:
                nets.append(net)
                holds.append((tick.ts - open_trade.entry_ts).total_seconds())
                open_trade = None
        positions = [] if open_trade is None else [{"symbol": open_trade.intent.symbol}]
        for eng in engines:
            for intent in eng.generate(tick, account_equity, positions):
                signals += 1
                res = cost_gate_intent(intent, gate, current_funding_rate)
                if not res.approved:
                    continue
                survived += 1
                if open_trade is None:
                    open_trade = _OpenTrade(intent, tick.mid, tick.ts,
                                            res.cost.total_cost_bps)
                    positions = [{"symbol": intent.symbol}]
    span = (ticks[-1].ts - ticks[0].ts).total_seconds() if len(ticks) >= 2 else 0.0
    return signals, survived, nets, holds, span


def _report_from(n_ticks, signals, survived, nets0, holds, span_sec, extra):
    nets = [n - extra for n in nets0]
    trades = len(nets)
    wins = sum(1 for n in nets if n > 0)
    avg = float(sum(nets) / trades) if trades else 0.0
    verdict = ("NO_TRADES" if trades == 0 else "POSITIVE" if avg > 1.0
               else "MARGINAL" if avg >= -1.0 else "NEGATIVE")
    return TickBacktestReport(
        ticks=n_ticks, span_hours=round(span_sec / 3600.0, 3),
        signals_generated=signals, cost_gate_survived=survived,
        survival_rate=round(survived / signals, 4) if signals else 0.0,
        trades=trades, wins=wins, losses=trades - wins,
        win_rate=round(wins / trades, 4) if trades else 0.0,
        avg_net_bps=round(avg, 3),
        median_net_bps=round(float(statistics.median(nets)), 3) if trades else 0.0,
        total_net_bps=round(float(sum(nets)), 3),
        avg_hold_seconds=round(sum(holds) / len(holds), 2) if holds else 0.0,
        trades_per_day=round(trades / (span_sec / 86400.0), 2) if span_sec > 0 else 0.0,
        verdict=verdict,
    )


class SlippageSweepRow(BaseModel):
    model_config = {"frozen": True}
    extra_cost_bps: float
    trades: int
    avg_net_bps: float
    win_rate: float
    verdict: str


class SlippageSweep(BaseModel):
    model_config = {"frozen": True}
    rows: list[SlippageSweepRow]
    #: extra slippage (bps) at which realized avg net crosses zero (linear interp);
    #: None if it never crosses in the grid. NEGATIVE-at-zero means already underwater.
    break_even_extra_bps: Optional[float] = None


def slippage_sweep(
    ticks: Sequence[TickSnapshot],
    engines: Sequence[SignalEngine],
    gate: CostGate,
    *,
    grid: Optional[Sequence[Decimal]] = None,
    account_equity: Decimal = Decimal("500"),
    current_funding_rate: object = Decimal("0"),
) -> SlippageSweep:
    """How much EXTRA slippage can the realized edge tolerate before avg net ≤ 0?
    Simulates once, then re-derives each grid point (extra slippage only shifts net)."""
    if grid is None:
        grid = [Decimal(x) for x in ("0", "1", "2", "4", "6", "8")]
    signals, survived, nets, holds, span = _simulate(
        ticks, engines, gate, account_equity, current_funding_rate)
    rows: list[SlippageSweepRow] = []
    for extra in grid:
        rep = _report_from(len(ticks), signals, survived, nets, holds, span, Decimal(str(extra)))
        rows.append(SlippageSweepRow(
            extra_cost_bps=float(extra), trades=rep.trades, avg_net_bps=rep.avg_net_bps,
            win_rate=rep.win_rate, verdict=rep.verdict))
    be: Optional[float] = None
    for a, b in zip(rows, rows[1:]):
        if a.avg_net_bps > 0 >= b.avg_net_bps:
            frac = a.avg_net_bps / (a.avg_net_bps - b.avg_net_bps)
            be = round(a.extra_cost_bps + frac * (b.extra_cost_bps - a.extra_cost_bps), 2)
            break
    return SlippageSweep(rows=rows, break_even_extra_bps=be)
