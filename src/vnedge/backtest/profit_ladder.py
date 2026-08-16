"""Profit Ladder Analyzer — where does unrealized P&L actually appear, and when
does it decay?

For every signal, walk the real tick path forward and sample the unrealized move
(bps, before cost) at fixed checkpoints (3s … 60m), plus time-to-first-profit and
time-to-MFE. Answers WHERE the profit steps up, when it peaks, and how fast it
collapses back into noise — the correct measurement to make BEFORE choosing a
max-hold / time-stop.

Diagnostic (signal quality), float internals, deterministic. Each checkpoint is
recorded ONCE per signal at the first tick at/after the mark (the user's skeleton
recorded at every tick past it — fixed here with a per-signal checkpoint pointer).
"""

from __future__ import annotations

import statistics as st
from decimal import Decimal
from typing import Optional, Sequence

from pydantic import BaseModel

from vnedge.strategy.signal_engine import SignalIntent, TickSnapshot

DEFAULT_CHECKPOINTS = (3, 5, 8, 10, 15, 20, 30, 60, 120, 180, 300, 600, 900, 1200, 1800, 3600)


class LadderCheckpoint(BaseModel):
    model_config = {"frozen": True}
    sec: int
    avg_unrealized_bps: float
    median_unrealized_bps: float
    pct_ever_positive: float          # reached >0 bps by this checkpoint
    pct_currently_positive: float     # >0 at this exact checkpoint
    avg_mfe_so_far_bps: float
    n_samples: int


class ProfitLadderReport(BaseModel):
    model_config = {"frozen": True}
    signals: int
    checkpoints: list[LadderCheckpoint]
    median_time_to_first_profit_sec: float
    median_time_to_mfe_sec: float
    p90_time_to_mfe_sec: float
    pct_trades_ever_profitable: float


def analyze_profit_ladder(
    ticks: Sequence[TickSnapshot],
    signals: Sequence[tuple[int, SignalIntent]],
    *,
    checkpoints: Sequence[int] = DEFAULT_CHECKPOINTS,
) -> ProfitLadderReport:
    mids = [float(t.mid) for t in ticks]
    tsec = [t.ts.timestamp() for t in ticks]
    n = len(ticks)
    cps = sorted(checkpoints)
    max_cp = cps[-1]
    acc = {cp: {"unreal": [], "mfe": [], "ever": 0, "still": 0, "n": 0} for cp in cps}
    t_first: list[float] = []
    t_mfe: list[float] = []
    ever_profitable = 0

    for i, intent in signals:
        entry = mids[i]
        sign = 1.0 if intent.side == "buy" else -1.0
        t0 = tsec[i]
        mfe = 0.0
        tmfe = 0.0
        first_profit: Optional[float] = None
        reached_pos = False
        cp_ptr = 0
        j = i + 1
        while j < n:
            dt = tsec[j] - t0
            if dt > max_cp:
                break
            move = (mids[j] - entry) / entry * 10000.0 * sign
            if move > mfe:
                mfe, tmfe = move, dt
            if move > 0 and first_profit is None:
                first_profit = dt
                reached_pos = True
            while cp_ptr < len(cps) and dt >= cps[cp_ptr]:   # record each cp ONCE, first tick past it
                a = acc[cps[cp_ptr]]
                a["unreal"].append(move)
                a["mfe"].append(mfe)
                a["n"] += 1
                if move > 0:
                    a["still"] += 1
                if reached_pos:
                    a["ever"] += 1
                cp_ptr += 1
            j += 1

        if reached_pos:
            ever_profitable += 1
            if first_profit is not None:
                t_first.append(first_profit)
        if mfe > 0:
            t_mfe.append(tmfe)

    def _p90(xs):
        return sorted(xs)[int(0.9 * (len(xs) - 1))] if xs else 0.0

    rows: list[LadderCheckpoint] = []
    for cp in cps:
        a = acc[cp]
        if a["n"] == 0:
            continue
        rows.append(LadderCheckpoint(
            sec=cp,
            avg_unrealized_bps=round(sum(a["unreal"]) / a["n"], 2),
            median_unrealized_bps=round(st.median(a["unreal"]), 2),
            pct_ever_positive=round(100 * a["ever"] / a["n"], 1),
            pct_currently_positive=round(100 * a["still"] / a["n"], 1),
            avg_mfe_so_far_bps=round(sum(a["mfe"]) / a["n"], 2),
            n_samples=a["n"],
        ))
    n_sig = len(signals) or 1
    return ProfitLadderReport(
        signals=len(signals),
        checkpoints=rows,
        median_time_to_first_profit_sec=round(st.median(t_first), 1) if t_first else 0.0,
        median_time_to_mfe_sec=round(st.median(t_mfe), 1) if t_mfe else 0.0,
        p90_time_to_mfe_sec=round(_p90(t_mfe), 1),
        pct_trades_ever_profitable=round(100 * ever_profitable / n_sig, 1),
    )
