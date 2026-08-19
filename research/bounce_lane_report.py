"""Run the maker and taker bounce lanes side by side.

The lanes are identical except for entry and fee, so the delta between them
IS the value of passive entry -- under an assumed maker rate the taker lane
does not need.  The sensitivity block shows where that assumption stops
paying, which is the number to check against a real venue schedule.

Usage:
    python -m research.bounce_lane_report --days 90
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics

from research.squeeze_trigger_replay import fetch
from vnedge.runtime.scanner_session import daily_returns_bps, summarize
from vnedge.strategy.bounce_lanes import LANES, MAKER_LANE, TAKER_LANE, maker_lane_at

UTC = dt.timezone.utc
WARMUP_DAYS = 12


def run_lane(lane, tapes: dict[str, list[tuple]], start_ms: int) -> list:
    trades: list = []
    for symbol, bars in tapes.items():
        trades += lane.session(symbol).run(bars, start_ms=start_ms)
    return trades


def row(label: str, trades: list, notional: float) -> dict:
    s = summarize(trades, notional)
    if not s["n"]:
        print(f"  {label:<30}{0:>5}")
        return s
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    gross = sum(t.gross_bps for t in trades) / s["n"]
    fee = sum(t.fee_bps for t in trades) / s["n"]
    print(
        f"  {label:<30}{s['n']:>5}{100 * s['wins'] / s['n']:>5.0f}%{pf:>7}"
        f"{s['net_bps']:>+9.0f}{gross:>+8.2f}{fee:>7.2f}"
        f"{s['net_usd']:>+9.0f}{s['max_dd_usd']:>9.0f}{s['psr']:>7.3f}"
    )
    return s


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--notional", type=float, default=3000.0)
    args = parser.parse_args()

    now = int(dt.datetime.now(UTC).timestamp() * 1000)
    end_ms = now - now % 300_000
    warm = end_ms - (args.days + WARMUP_DAYS) * 86_400_000
    start_ms = end_ms - args.days * 86_400_000
    tapes = {s.strip(): fetch(s.strip(), "5m", warm, end_ms)
             for s in args.symbols.split(",")}

    print(f"\nBOUNCE LANES  {args.days}d  {', '.join(tapes)}  ${args.notional:.0f} notional")
    print(f"\n  {'lane':<30}{'n':>5}{'WR':>6}{'PF':>7}{'net bps':>9}"
          f"{'gr/tr':>8}{'fee/tr':>7}{'net $':>9}{'maxDD':>9}{'PSR':>7}")

    books = {}
    for lane in LANES:
        books[lane.lane_id] = run_lane(lane, tapes, start_ms)
        row(lane.lane_id, books[lane.lane_id], args.notional)

    taker = summarize(books[TAKER_LANE.lane_id], args.notional)
    maker = summarize(books[MAKER_LANE.lane_id], args.notional)
    if taker["n"] and maker["n"]:
        print(f"\n  passive-entry delta: {maker['net_bps'] - taker['net_bps']:+.0f} bps "
              f"({maker['n']} maker fills vs {taker['n']} taker entries) "
              f"-- rests on an ASSUMED {MAKER_LANE.maker_bps} bps maker rate")

    print(f"\n  maker-fee sensitivity (same fills, fee varied)")
    print(f"\n  {'assumed maker bps':<30}{'n':>5}{'WR':>6}{'PF':>7}{'net bps':>9}"
          f"{'gr/tr':>8}{'fee/tr':>7}{'net $':>9}{'maxDD':>9}{'PSR':>7}")
    for bps in (0.0, 1.0, 2.0, 4.0, 5.9):
        trades = run_lane(maker_lane_at(bps), tapes, start_ms)
        row(f"maker @ {bps:.1f} bps", trades, args.notional)

    for lane_id, trades in books.items():
        if not trades:
            continue
        months: dict[str, float] = {}
        for t in trades:
            key = t.entry_time.strftime("%Y-%m")
            months[key] = months.get(key, 0.0) + t.net_bps
        daily = daily_returns_bps(trades)
        print(f"\n  {lane_id}: months "
              + " ".join(f"{k}={v:+.0f}" for k, v in sorted(months.items()))
              + f"  | days traded {sum(1 for d in daily if d)} of {len(daily)}"
              + f"  | median hold {statistics.median([t.held_bars for t in trades]) * 5:.0f}m")


if __name__ == "__main__":
    main()
