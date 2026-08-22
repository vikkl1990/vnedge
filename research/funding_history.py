"""Funding is a signed, measured cost — not a constant.

Multi-day holds cross many funding stamps, and a flat per-period assumption is
wrong in two ways at once:

* magnitude — Binance BTC/ETH funding averaged 2.5-3.5 bps per stamp through
  2020-mid-2021 and 0.6-0.7 bps through the 2021-2023 window. A single
  constant misprices one of those by a factor of four;
* sign — a POSITIVE rate means longs pay shorts. Charging both sides equally
  bills a short for income it actually receives.

At ~15 stamps per five-day hold this dominates the round-trip fee, so any
strategy holding longer than a few hours must price it from the series.

Usage:
    python -m research.funding_history --symbols BTCUSDT,ETHUSDT \
        --from 2020-01-01 --to 2021-06-30
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import time
import urllib.request

UTC = dt.timezone.utc
FAPI = "https://fapi.binance.com/fapi/v1/fundingRate"


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    """Every funding stamp in the window, oldest first."""
    out: list[tuple[int, float]] = []
    cursor = start_ms
    while cursor < end_ms:
        url = f"{FAPI}?symbol={symbol}&startTime={cursor}&endTime={end_ms}&limit=1000"
        with urllib.request.urlopen(url, timeout=30) as response:
            rows = json.load(response)
        if not rows:
            break
        out += [(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows]
        nxt = int(rows[-1]["fundingTime"]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.15)
    return out


def funding_cost_bps(stamps: list[tuple[int, float]], side: str,
                     entry_ms: int, exit_ms: int) -> float:
    """Signed funding paid over one hold, in bps of notional.

    Positive is a COST. Longs pay a positive rate; shorts receive it.
    """
    total = sum(rate for ts, rate in stamps if entry_ms < ts <= exit_ms) * 1e4
    return total if side == "long" else -total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    args = parser.parse_args()

    start = int(dt.datetime.fromisoformat(args.start).replace(tzinfo=UTC).timestamp() * 1000)
    end = int(dt.datetime.fromisoformat(args.end).replace(tzinfo=UTC).timestamp() * 1000)

    print(f"\nFUNDING  {args.start} -> {args.end}")
    print(f"\n  {'symbol':<10}{'stamps':>8}{'mean bps':>11}{'median':>9}"
          f"{'p90':>8}{'max':>8}{'positive':>10}{'per 5d hold':>13}")
    for symbol in (s.strip() for s in args.symbols.split(",")):
        stamps = fetch_funding(symbol, start, end)
        if not stamps:
            print(f"  {symbol:<10}{0:>8}")
            continue
        bps = sorted(r * 1e4 for _, r in stamps)
        mean = statistics.fmean(bps)
        positive = sum(1 for b in bps if b > 0)
        print(f"  {symbol:<10}{len(stamps):>8}{mean:>+11.3f}"
              f"{statistics.median(bps):>+9.3f}{bps[9 * len(bps) // 10]:>+8.3f}"
              f"{bps[-1]:>+8.2f}{100 * positive / len(bps):>9.0f}%"
              f"{mean * 15:>+13.1f}")
    print("\n  'per 5d hold' assumes 15 stamps and applies to a LONG; a short of the")
    print("  same length receives that amount instead of paying it.")


if __name__ == "__main__":
    main()
