"""Turn a lane's bps book into an account path, and test it for ruin.

A profit factor says whether an edge exists.  It says nothing about whether a
given sizing survives that edge's losing runs, and those are different
questions -- a lane can be PF > 1 and still bust the account that traded it.

Two things are reported:

* the realised path under fixed leverage versus the repo's risk-per-trade
  rule ("size comes from risk-per-trade and stop distance, never leverage");
* ruin frequency across resampled orderings of the SAME trades.  Under fixed
  notional the final equity is order-independent, so any spread in outcomes
  is purely the chance of going bust part-way through -- exactly the risk a
  backtest's single ordering hides.

Usage:
    python -m research.account_path --days 30 --margin 100 --leverage 30
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import statistics

from research.squeeze_trigger_replay import fetch
from vnedge.strategy.bounce_lanes import LANES

UTC = dt.timezone.utc
WARMUP_DAYS = 20


def load(symbols: list[str], end_ms: int, days: int) -> dict:
    start = end_ms - int((days + WARMUP_DAYS) * 86_400_000)
    return {s: fetch(s, "5m", start, end_ms) for s in symbols}


def book(lane, tapes: dict, start_ms: int) -> list[tuple]:
    """(net_bps, stop_fraction) per trade, ordered by entry."""
    rows: list[tuple] = []
    for symbol, bars in tapes.items():
        risks: dict[int, float] = {}
        session = lane.session(
            symbol,
            on_fire=lambda d, r=risks: r.__setitem__(
                d["bar"], abs(d["entry"] - d["stop"]) / d["entry"]
            ),
        )
        for trade in session.run(bars, start_ms=start_ms):
            stop = risks.get(trade.entry_index)
            if stop:
                rows.append((trade.entry_ts_ms, trade.net_bps, stop))
    rows.sort()
    return [(bps, stop) for _, bps, stop in rows]


def walk(rows, margin: float, *, leverage: float | None, risk_pct: float | None,
         cap: float = 30.0) -> dict:
    equity = peak = margin
    max_dd = 0.0
    worst = 0.0
    ruined = False
    for bps, stop in rows:
        if risk_pct is None:
            notional = margin * (leverage or 0.0)   # fixed notional, not equity-scaled
        else:
            notional = min((equity * risk_pct) / stop, equity * cap) if stop > 0 else 0.0
        pnl = bps * notional / 1e4
        worst = min(worst, pnl)
        equity += pnl
        if equity <= 0:
            return {"equity": 0.0, "max_dd": max_dd, "worst": worst, "ruined": True}
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {"equity": equity, "max_dd": max_dd, "worst": worst, "ruined": ruined}


def ruin_rate(rows, margin: float, *, leverage: float | None,
              risk_pct: float | None, trials: int, seed: int) -> float:
    rng = random.Random(seed)
    ruins = 0
    for _ in range(trials):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        if walk(shuffled, margin, leverage=leverage, risk_pct=risk_pct)["ruined"]:
            ruins += 1
    return 100.0 * ruins / trials


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--margin", type=float, default=100.0)
    p.add_argument("--leverage", type=float, default=30.0)
    p.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    p.add_argument("--trials", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=20260820)
    args = p.parse_args()

    now = int(dt.datetime.now(UTC).timestamp() * 1000)
    end_ms = now - now % 300_000
    start_ms = end_ms - args.days * 86_400_000
    tapes = load([s.strip() for s in args.symbols.split(",")], end_ms, args.days)

    notional = args.margin * args.leverage
    print(f"\nACCOUNT PATH  {args.days}d  ${args.margin:.0f} margin x "
          f"{args.leverage:.0f}x = ${notional:,.0f} notional")
    print(f"  liquidation at a {100 / args.leverage:.2f}% adverse move "
          f"({100 * 100 / args.leverage:.0f} bps), before maintenance margin and funding")

    for lane in LANES:
        rows = book(lane, tapes, start_ms)
        if not rows:
            print(f"\n  {lane.lane_id}: no trades")
            continue
        stops = [s for _, s in rows]
        print(f"\n  {lane.lane_id}   n={len(rows)}  "
              f"median stop {100 * statistics.median(stops):.2f}%")
        print(f"    {'sizing':<32}{'end':>10}{'maxDD':>8}{'worst':>9}{'ruin %':>9}")
        plans = [(f"fixed {args.leverage:.0f}x", args.leverage, None),
                 ("risk 1% of equity", None, 0.01),
                 ("risk 2% of equity", None, 0.02),
                 ("risk 5% of equity", None, 0.05)]
        for label, lev, risk in plans:
            res = walk(rows, args.margin, leverage=lev, risk_pct=risk)
            rate = ruin_rate(rows, args.margin, leverage=lev, risk_pct=risk,
                             trials=args.trials, seed=args.seed)
            end = "RUINED" if res["ruined"] else f"{res['equity']:+.2f}"
            print(f"    {label:<32}{end:>10}{res['max_dd']:>8.0f}"
                  f"{res['worst']:>9.2f}{rate:>8.1f}%")


if __name__ == "__main__":
    main()
