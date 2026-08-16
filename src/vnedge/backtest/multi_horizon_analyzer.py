"""Multi-Horizon Holding & Excursion Analyzer.

For every signal an engine emits, walk the real tick path forward under multiple
maximum-holding horizons and record, per horizon: did the stop or take-profit hit
first, the max favorable / adverse excursion (MFE / MAE) and when the MFE peaked,
and the realized net (after a fixed round-trip cost) if force-exited at the horizon.

Answers: how long the moves actually live, whether ANY horizon has residual net
edge after cost, and whether the ~40-45s cap is too long/short/irrelevant.

Diagnostic, NOT a P&L sim — every signal is analysed independently (overlaps
allowed) → SIGNAL QUALITY, not a tradeable curve. Float internals (a diagnostic
doesn't need Decimal precision; Decimal per-tick over millions of ticks is minutes
slow). STOP wins ties. The stop/tp used are the intent's own, unless overridden.
"""

from __future__ import annotations

import statistics as st
from decimal import Decimal
from typing import Optional, Sequence

from pydantic import BaseModel

from vnedge.strategy.signal_engine import SignalEngine, SignalIntent, TickSnapshot

DEFAULT_HORIZONS = (60, 300, 1200, 1800, 3600)   # 1m, 5m, 20m, 30m, 60m


class HorizonSummary(BaseModel):
    model_config = {"frozen": True}
    horizon_sec: int
    n_signals: int
    pct_hit_tp: float
    pct_hit_sl: float
    pct_timed_out: float
    median_time_to_mfe_sec: float
    p90_time_to_mfe_sec: float
    avg_mfe_bps: float           # gross favorable move (cost-free)
    avg_mae_bps: float           # gross adverse move
    avg_final_net_bps: float     # after cost, all signals
    avg_final_net_winners: float
    median_final_net_bps: float


class MultiHorizonReport(BaseModel):
    model_config = {"frozen": True}
    signals: int
    cost_bps: float
    summaries: list[HorizonSummary]


def signals_with_index(
    ticks: Sequence[TickSnapshot],
    engines: Sequence[SignalEngine],
    account_equity: Decimal = Decimal("500"),
) -> list[tuple[int, SignalIntent]]:
    """Every signal the engines emit, tagged with its tick index (open_positions
    always empty → the FULL candidate set; signal-quality, not flat-only P&L). The
    index avoids an O(signals×ticks) entry search later."""
    out: list[tuple[int, SignalIntent]] = []
    for i, tick in enumerate(ticks):
        for eng in engines:
            for intent in eng.generate(tick, account_equity, []):
                out.append((i, intent))
    return out


def analyze_horizons(
    ticks: Sequence[TickSnapshot],
    signals: Sequence[tuple[int, SignalIntent]],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    cost_bps: float = 14.0,
    stop_bps_override: Optional[float] = None,
    tp_bps_override: Optional[float] = None,
) -> MultiHorizonReport:
    mids = [float(t.mid) for t in ticks]
    tsec = [t.ts.timestamp() for t in ticks]
    n = len(ticks)
    H = sorted(horizons)
    max_h = H[-1]
    acc = {h: {"tp": 0, "sl": 0, "to": 0, "mfe": [], "mae": [], "tmfe": [], "net": []} for h in H}

    for i, intent in signals:
        entry = mids[i]
        sign = 1.0 if intent.side == "buy" else -1.0
        stop = stop_bps_override if stop_bps_override is not None else float(intent.stop_distance_bps)
        tp = (tp_bps_override if tp_bps_override is not None
              else float(intent.take_profit_bps) if intent.take_profit_bps is not None else None)
        t0 = tsec[i]
        mfe = mae = 0.0
        tmfe = 0.0
        t_sl = t_tp = None
        move_at = {h: 0.0 for h in H}
        hp = 0
        prev_move = 0.0
        j = i + 1
        while j < n:
            dt = tsec[j] - t0
            if dt > max_h:
                break
            move = (mids[j] - entry) / entry * 10000.0 * sign
            if move > mfe:
                mfe, tmfe = move, dt
            if move < mae:
                mae = move
            if t_sl is None and move <= -stop:
                t_sl = dt
            if tp is not None and t_tp is None and move >= tp:
                t_tp = dt
            while hp < len(H) and dt > H[hp]:
                move_at[H[hp]] = prev_move
                hp += 1
            prev_move = move
            j += 1
        while hp < len(H):
            move_at[H[hp]] = prev_move
            hp += 1

        for h in H:
            if t_sl is not None and t_sl <= h and (t_tp is None or t_sl <= t_tp):
                outcome, exit_bps = "sl", -stop
            elif t_tp is not None and t_tp <= h:
                outcome, exit_bps = "tp", tp
            else:
                outcome, exit_bps = "to", move_at[h]
            a = acc[h]
            a[outcome] += 1
            a["mfe"].append(mfe)
            a["mae"].append(mae)
            a["tmfe"].append(tmfe)
            a["net"].append(exit_bps - cost_bps)

    def _p90(xs):
        return sorted(xs)[int(0.9 * (len(xs) - 1))] if xs else 0.0

    summaries: list[HorizonSummary] = []
    for h in H:
        a = acc[h]
        tot = a["tp"] + a["sl"] + a["to"]
        if tot == 0:
            continue
        nets = a["net"]
        wins = [x for x in nets if x > 0]
        summaries.append(HorizonSummary(
            horizon_sec=h, n_signals=tot,
            pct_hit_tp=round(100 * a["tp"] / tot, 1),
            pct_hit_sl=round(100 * a["sl"] / tot, 1),
            pct_timed_out=round(100 * a["to"] / tot, 1),
            median_time_to_mfe_sec=round(st.median(a["tmfe"]), 1),
            p90_time_to_mfe_sec=round(_p90(a["tmfe"]), 1),
            avg_mfe_bps=round(sum(a["mfe"]) / tot, 2),
            avg_mae_bps=round(sum(a["mae"]) / tot, 2),
            avg_final_net_bps=round(sum(nets) / tot, 2),
            avg_final_net_winners=round(sum(wins) / len(wins), 2) if wins else 0.0,
            median_final_net_bps=round(st.median(nets), 2),
        ))
    return MultiHorizonReport(signals=len(signals), cost_bps=cost_bps, summaries=summaries)
