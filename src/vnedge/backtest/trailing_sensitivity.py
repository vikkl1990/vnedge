"""Trailing-stop parameter sensitivity sweep.

Holds the entry logic + CostGate fixed and sweeps ONLY the exit: initial stop,
trail activation (profit reached before trailing starts), trail distance, and time
cap. The point is not to find the single best number — it's to see whether a STABLE
PLATEAU of positive configs exists (real edge) or just one sharp peak (a curve-fit
artifact). ``pct_positive`` is the plateau indicator.

Note: the ``mfe`` and ``price`` trail types are identical once the stop is ratcheted
(max of ``price − distance`` over time == ``mfe − distance``), so we keep ``mfe``.
Float internals, index-based (no O(signals×ticks) search), deterministic, STOP wins ties.
"""

from __future__ import annotations

import statistics as st
from itertools import product
from typing import Optional, Sequence

from pydantic import BaseModel

from vnedge.strategy.signal_engine import SignalIntent, TickSnapshot


class TrailParams(BaseModel):
    model_config = {"frozen": True}
    initial_stop_bps: float
    trail_activation_bps: float
    trail_distance_bps: float
    time_cap_sec: int
    name: str = ""


class TrailResult(BaseModel):
    model_config = {"frozen": True}
    name: str
    n_trades: int
    pct_initial_stop: float
    pct_trail: float
    pct_time: float
    avg_net_bps: float
    median_net_bps: float
    win_rate: float
    profit_factor: float
    avg_hold_sec: float


class TrailingSensitivityReport(BaseModel):
    model_config = {"frozen": True}
    cost_bps: float
    n_configs: int
    pct_positive: float                      # share of the grid with avg_net > 0 (plateau indicator)
    results: list[TrailResult]               # sorted by avg_net desc
    best_by_avg_net: Optional[TrailResult] = None
    best_by_profit_factor: Optional[TrailResult] = None


def default_grid(
    initial_stops=(16.0, 20.0, 25.0),
    activations=(8.0, 10.0, 12.0),
    distances=(8.0, 10.0, 12.0),
    time_caps=(900, 1200),
) -> list[TrailParams]:
    grid = []
    for i, a, d, t in product(initial_stops, activations, distances, time_caps):
        grid.append(TrailParams(initial_stop_bps=i, trail_activation_bps=a,
                                trail_distance_bps=d, time_cap_sec=t,
                                name=f"i{i:g}_a{a:g}_d{d:g}_t{t}"))
    return grid


def _simulate(entry, sign, mids, tsec, i, n, p: TrailParams) -> tuple[float, str, float]:
    """Return (gross_bps, reason, hold_sec) for one signal under one param set."""
    t0 = tsec[i]
    stop_level = -p.initial_stop_bps
    mfe = 0.0
    trailing = False
    j = i + 1
    move = 0.0
    while j < n:
        dt = tsec[j] - t0
        move = (mids[j] - entry) / entry * 10000.0 * sign
        if move > mfe:
            mfe = move
        if not trailing and mfe >= p.trail_activation_bps:
            trailing = True
        if trailing:
            stop_level = max(stop_level, mfe - p.trail_distance_bps)   # ratchet up only
        if move <= stop_level:                                        # stop wins ties
            return stop_level, ("trail" if trailing else "initial_stop"), dt
        if dt >= p.time_cap_sec:
            return move, "time", dt
        j += 1
    return move, "end", (tsec[min(j, n - 1)] - t0) if n else 0.0


def sweep_trailing(
    ticks: Sequence[TickSnapshot],
    signals: Sequence[tuple[int, SignalIntent]],
    grid: Sequence[TrailParams],
    *,
    fee_bps: float = 5.0,
    slip_bps: float = 2.0,
) -> TrailingSensitivityReport:
    mids = [float(t.mid) for t in ticks]
    tsec = [t.ts.timestamp() for t in ticks]
    n = len(ticks)
    cost = fee_bps + slip_bps
    results: list[TrailResult] = []

    for p in grid:
        nets: list[float] = []
        holds: list[float] = []
        reasons = {"initial_stop": 0, "trail": 0, "time": 0, "end": 0}
        for i, intent in signals:
            entry = mids[i]
            sign = 1.0 if intent.side == "buy" else -1.0
            gross, reason, hold = _simulate(entry, sign, mids, tsec, i, n, p)
            reasons[reason] += 1
            nets.append(gross - cost)
            holds.append(hold)
        m = len(nets)
        if m == 0:
            continue
        wins = [x for x in nets if x > 0]
        losses = [-x for x in nets if x <= 0]
        gw, gl = sum(wins), sum(losses) or 1e-9
        results.append(TrailResult(
            name=p.name, n_trades=m,
            pct_initial_stop=round(100 * reasons["initial_stop"] / m, 1),
            pct_trail=round(100 * reasons["trail"] / m, 1),
            pct_time=round(100 * (reasons["time"] + reasons["end"]) / m, 1),
            avg_net_bps=round(sum(nets) / m, 2),
            median_net_bps=round(st.median(nets), 2),
            win_rate=round(len(wins) / m, 3),
            profit_factor=round(gw / gl, 2),
            avg_hold_sec=round(sum(holds) / m, 1),
        ))

    results.sort(key=lambda r: r.avg_net_bps, reverse=True)
    pos = sum(1 for r in results if r.avg_net_bps > 0)
    return TrailingSensitivityReport(
        cost_bps=cost, n_configs=len(results),
        pct_positive=round(100 * pos / len(results), 1) if results else 0.0,
        results=results,
        best_by_avg_net=results[0] if results else None,
        best_by_profit_factor=max(results, key=lambda r: r.profit_factor) if results else None,
    )
