"""Hybrid arming: coil breaks + ignition thrusts through one shared plane.

RESEARCH_ONLY ablation tool.  The 48h journal comparison exposed two
complementary failure modes:

- the coil scanner is right when it trades and absent when it matters
  (armed on 1.0% of bars in the 16-18 Aug window);
- the fee-wall observer is present for ignitions and wrong most of the
  time (PF 0.61 live), because it chases the spike top and exits badly.

This tool tests the obvious synthesis: keep the observer's *breadth* of
arming but route it through the discipline that works -- TriggerEngine's
chase cap, one-position rule, day budget and cooldowns, plus ExitEngine's
level-anchored stop, failed-breakout kill and trail.

Arms:
  COIL     rank <= 0.20 box break            (current S1)
  IGNITION thrust bar (body >= 60% of range) closing beyond the recent
           box on >= ignition_vol_mult volume -- the observer's trigger,
           but fired through the same gates and managed by the same exit.

Modes: coil | ignition | hybrid, so each arm's contribution is separable.
This is the A0/A2-class ablation; it decides nothing on its own and any
promotion still requires a sealed window.

Usage:
    python -m research.hybrid_arm_replay --days 90
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics

from research.squeeze_trigger_replay import (
    ATR_PERIOD,
    BREAK_BUFFER_BPS,
    COMPRESSION_BARS,
    COMPRESSION_THRESHOLD,
    RANK_LOOKBACK,
    SCALPER_FREE_CLOSE_BARS,
    TAKER_BPS,
    VOL_LOOKBACK,
    VWAP_BARS,
    fetch,
)
from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.execution.trigger_engine import ArmState, TriggerConfig, TriggerEngine

UTC = dt.timezone.utc

IGNITION_BODY_FRAC = 0.60
IGNITION_VOL_MULT = 2.5
IGNITION_BOX_BARS = 24  # the level an ignition must clear (2h)


def replay(symbol: str, bars: list[tuple], start_ms: int, mode: str) -> list[dict]:
    trigger = TriggerEngine(config=TriggerConfig())
    exits = ExitEngine(config=ExitConfig())
    trades: list[dict] = []
    meta: dict | None = None
    episode = 0
    prev_compressed = False
    rank_window: list[float] = []
    pv = vv = 0.0

    for i in range(len(bars)):
        ts = bars[i][0]
        if i >= 1:
            j = i - 1
            pv += bars[j][4] * bars[j][5]
            vv += bars[j][5]
            if i - 1 >= VWAP_BARS:
                k = i - 1 - VWAP_BARS
                pv -= bars[k][4] * bars[k][5]
                vv -= bars[k][5]
        vwap = pv / vv if vv > 0 else None
        if i < COMPRESSION_BARS + 1:
            continue

        box_high = max(b[2] for b in bars[i - COMPRESSION_BARS : i])
        box_low = min(b[3] for b in bars[i - COMPRESSION_BARS : i])
        span = (box_high - box_low) / bars[i - 1][4]
        rank_window.append(span)
        if len(rank_window) > RANK_LOOKBACK:
            rank_window.pop(0)
        if len(rank_window) < RANK_LOOKBACK:
            continue
        rank = sum(1 for x in rank_window if x < span) / len(rank_window)
        compressed = rank <= COMPRESSION_THRESHOLD
        if compressed and not prev_compressed:
            episode += 1
        prev_compressed = compressed

        atr = statistics.mean(
            max(
                bars[k][2] - bars[k][3],
                abs(bars[k][2] - bars[k - 1][4]),
                abs(bars[k][3] - bars[k - 1][4]),
            )
            for k in range(i - ATR_PERIOD, i)
        )
        vol_ma = statistics.mean(b[5] for b in bars[i - VOL_LOOKBACK : i])

        if meta is not None:
            decision = exits.on_bar(
                high=bars[i][2], low=bars[i][3], close=bars[i][4], atr=atr, bar_index=i
            )
            if decision is not None:
                held = i - meta["bar"]
                side = meta["side"]
                gross = (
                    (decision.price / meta["entry"] - 1)
                    if side == "long"
                    else (1 - decision.price / meta["entry"])
                ) * 1e4
                fee = TAKER_BPS + (0.0 if held <= SCALPER_FREE_CLOSE_BARS else TAKER_BPS)
                trades.append(
                    {
                        "arm": meta["arm"], "ts": bars[meta["bar"]][0], "side": side,
                        "net_bps": gross - fee, "reason": decision.reason, "held": held,
                    }
                )
                trigger.notify_flat(i, won=decision.won)
                meta = None
            continue
        if ts < start_ms or vol_ma <= 0 or atr <= 0:
            continue

        # --- select an arm -------------------------------------------------
        arm_kind: str | None = None
        arm_high, arm_low, arm_episode = box_high, box_low, episode
        if mode in ("coil", "hybrid") and compressed:
            arm_kind = "coil"
        if arm_kind is None and mode in ("ignition", "hybrid"):
            body = abs(bars[i][4] - bars[i][1])
            rng = bars[i][2] - bars[i][3]
            thrust = rng > 0 and body >= IGNITION_BODY_FRAC * rng
            loud = bars[i][5] >= IGNITION_VOL_MULT * vol_ma
            if thrust and loud:
                arm_kind = "ignition"
                arm_high = max(b[2] for b in bars[i - IGNITION_BOX_BARS : i])
                arm_low = min(b[3] for b in bars[i - IGNITION_BOX_BARS : i])
                # ignition arms are their own episode so the coil latch cannot
                # burn them, and vice versa
                arm_episode = -(i + 1)
        if arm_kind is None:
            continue

        fire = trigger.try_fire(
            arm=ArmState(
                episode_id=arm_episode, box_high=arm_high, box_low=arm_low,
                compressed=True, atr=atr, vol_ma=vol_ma, prev_close=bars[i - 1][4],
            ),
            high=bars[i][2], low=bars[i][3], close=bars[i][4], volume=bars[i][5],
            vwap=vwap, bar_index=i, bar_ts_ms=ts,
        )
        if fire is not None:
            exits.open_from_fire(
                side=fire.side, entry=fire.entry, stop=fire.stop, risk=fire.risk,
                box_edge=fire.box_edge, entry_bar=i,
            )
            meta = {"side": fire.side, "entry": fire.entry, "bar": i, "arm": arm_kind}
    return trades


def stats(trades: list[dict], notional: float) -> str:
    if not trades:
        return f"{0:>5}{'-':>7}{0.0:>10.0f}{0.0:>10.2f}"
    wins = sum(1 for t in trades if t["net_bps"] > 0)
    gw = sum(t["net_bps"] for t in trades if t["net_bps"] > 0)
    gl = -sum(t["net_bps"] for t in trades if t["net_bps"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    net = sum(t["net_bps"] for t in trades)
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    return f"{len(trades):>5}{pf_s:>7}{net:>10.0f}{net * notional / 1e4:>10.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--notional", type=float, default=3000.0)
    args = parser.parse_args()

    now = int(dt.datetime.now(UTC).timestamp() * 1000)
    end = now - now % 300_000
    tapes = {
        s.strip(): fetch(s.strip(), "5m", end - (args.days + 9) * 86_400_000, end)
        for s in args.symbols.split(",")
    }

    for label, days in (("48h", 2), ("7d", 7), ("30d", 30), (f"{args.days}d", args.days)):
        start = end - days * 86_400_000
        print(f"\n== {label}")
        print(f"   {'mode':<12}{'n':>5}{'PF':>7}{'net bps':>10}{'net $':>10}   by arm")
        for mode in ("coil", "ignition", "hybrid"):
            rows: list[dict] = []
            for bars in tapes.values():
                rows += replay("x", bars, start, mode)
            detail = ""
            if mode == "hybrid" and rows:
                for kind in ("coil", "ignition"):
                    sub = [t for t in rows if t["arm"] == kind]
                    if sub:
                        detail += f"  {kind}: n={len(sub)} net={sum(t['net_bps'] for t in sub):+.0f}"
            print(f"   {mode:<12}{stats(rows, args.notional)}{detail}")


if __name__ == "__main__":
    main()
