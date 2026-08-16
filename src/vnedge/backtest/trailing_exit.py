"""Trailing / wider-stop exit simulation — harvest the breakout asymmetry.

The profit ladder showed breakouts have real drift but the winners run far (MFE
+25→+58bps) while a tight stop cuts 58% of trades before continuation. This models
the exit that fits that shape: a WIDER initial stop (give noise room), a chandelier
TRAIL that arms after a delay (lets winners run, ratchets the stop up behind the
MFE), and a hard TIME CAP (the edge decays by 30-60min). Realized net = the exit's
gross move − a fixed round-trip cost. STOP wins ties. Deterministic.
"""

from __future__ import annotations

import statistics as st
from decimal import Decimal
from typing import Optional, Sequence

from pydantic import BaseModel

from vnedge.strategy.signal_engine import SignalIntent, TickSnapshot


class TrailingExitReport(BaseModel):
    model_config = {"frozen": True}
    trades: int
    init_stop_bps: float
    trail_bps: float
    arm_sec: int
    time_cap_sec: int
    cost_bps: float
    avg_net_bps: float
    median_net_bps: float
    win_rate: float
    pct_stop: float
    pct_trail: float
    pct_time: float
    avg_win_bps: float
    avg_loss_bps: float
    verdict: str


def trailing_exit_backtest(
    ticks: Sequence[TickSnapshot],
    signals: Sequence[tuple[int, SignalIntent]],
    *,
    init_stop_bps: float = 40.0,
    trail_bps: float = 12.0,
    arm_sec: int = 300,
    time_cap_sec: int = 1200,
    cost_bps: float = 14.0,
) -> TrailingExitReport:
    mids = [float(t.mid) for t in ticks]
    tsec = [t.ts.timestamp() for t in ticks]
    n = len(ticks)
    nets: list[float] = []
    reasons = {"stop": 0, "trail": 0, "time": 0, "end": 0}

    for i, intent in signals:
        entry = mids[i]
        sign = 1.0 if intent.side == "buy" else -1.0
        t0 = tsec[i]
        mfe = 0.0
        gross: Optional[float] = None
        reason = "end"
        last_move = 0.0
        j = i + 1
        while j < n:
            dt = tsec[j] - t0
            move = (mids[j] - entry) / entry * 10000.0 * sign
            last_move = move
            if move > mfe:
                mfe = move
            if move <= -init_stop_bps:                       # hard wider stop (wins ties)
                gross, reason = -init_stop_bps, "stop"
                break
            if dt >= arm_sec and move <= mfe - trail_bps:     # armed chandelier trail
                gross, reason = mfe - trail_bps, "trail"
                break
            if dt >= time_cap_sec:                            # time cap
                gross, reason = move, "time"
                break
            j += 1
        if gross is None:
            gross = last_move
        reasons[reason] += 1
        nets.append(gross - cost_bps)

    trades = len(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    avg = sum(nets) / trades if trades else 0.0
    verdict = ("NO_TRADES" if trades == 0 else "POSITIVE" if avg > 1.0
               else "MARGINAL" if avg >= -1.0 else "NEGATIVE")
    return TrailingExitReport(
        trades=trades, init_stop_bps=init_stop_bps, trail_bps=trail_bps,
        arm_sec=arm_sec, time_cap_sec=time_cap_sec, cost_bps=cost_bps,
        avg_net_bps=round(avg, 2),
        median_net_bps=round(st.median(nets), 2) if trades else 0.0,
        win_rate=round(len(wins) / trades, 4) if trades else 0.0,
        pct_stop=round(100 * reasons["stop"] / trades, 1) if trades else 0.0,
        pct_trail=round(100 * reasons["trail"] / trades, 1) if trades else 0.0,
        pct_time=round(100 * reasons["time"] / trades, 1) if trades else 0.0,
        avg_win_bps=round(sum(wins) / len(wins), 2) if wins else 0.0,
        avg_loss_bps=round(sum(losses) / len(losses), 2) if losses else 0.0,
        verdict=verdict,
    )
