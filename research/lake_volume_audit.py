"""Audit recorded trade volume against the venue's own bars.

Row-level duplicate counting cannot answer whether a tick lake is healthy.
The recorder dedupes by trade id and the parquet does not store that id, so
distinct trades sharing a millisecond, price and size look identical -- a
24-28% "duplicate" rate is normal and means nothing on its own.

Total volume against the venue's klines is the test that separates the two
failure modes that actually matter:

    ratio ~ 1.0   healthy
    ratio ~ 2.0   a stream written twice (two recorders owning one partition)
    ratio < 1.0   coverage loss (recorder down, restarting, or disconnected)

Usage:
    python -m research.lake_volume_audit --day 20260820 --hours 6
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import urllib.request

UTC = dt.timezone.utc
FAPI = "https://fapi.binance.com/fapi/v1/klines"
#: Websocket streams drop the occasional print; below this the gap is real.
HEALTHY_FLOOR = 0.97
DUPLICATE_FLOOR = 1.5


def venue_volume(symbol: str, start_ms: int) -> float | None:
    url = f"{FAPI}?symbol={symbol}&interval=1h&startTime={start_ms}&limit=1"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return float(json.load(response)[0][5])
    except Exception:
        return None


def lake_volume(root: str, exchange: str, symbol: str, day: str,
                start_ms: int, end_ms: int) -> tuple[float, int]:
    import pandas as pd

    pattern = (f"{root}/ticks/exchange={exchange}/symbol={symbol}"
               f"/stream=trades/{day}/*.parquet")
    files = sorted(glob.glob(pattern))
    if not files:
        return 0.0, 0
    frame = pd.concat(
        [pd.read_parquet(f, columns=["ts_ms", "amount"]) for f in files],
        ignore_index=True,
    )
    window = frame[(frame.ts_ms >= start_ms) & (frame.ts_ms < end_ms)]
    return float(window.amount.sum()), len(window)


def classify(ratio: float) -> str:
    if ratio >= DUPLICATE_FLOOR:
        return "DUPLICATED"
    if ratio >= HEALTHY_FLOOR:
        return "ok"
    return "COVERAGE LOSS"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default=dt.datetime.now(UTC).strftime("%Y%m%d"))
    parser.add_argument("--hours", type=int, default=6)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--exchange", default="binanceusdm")
    parser.add_argument("--data-root", default="data")
    args = parser.parse_args()

    day = dt.datetime.strptime(args.day, "%Y%m%d").replace(tzinfo=UTC)
    now = dt.datetime.now(UTC)
    findings = []
    for symbol in (s.strip() for s in args.symbols.split(",")):
        print(f"\n{symbol}")
        print(f"  {'hour (UTC)':<16}{'lake':>15}{'venue':>15}{'ratio':>8}  state")
        for hour in range(24):
            start = day + dt.timedelta(hours=hour)
            if start >= now:
                break
            end = start + dt.timedelta(hours=1)
            if end > now:
                break  # partial hour: the ratio would be meaningless
            if hour < 24 - args.hours:
                continue
            start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
            lake, rows = lake_volume(args.data_root, args.exchange, symbol,
                                     args.day, start_ms, end_ms)
            if rows < 100:
                continue
            venue = venue_volume(symbol, start_ms)
            if not venue:
                print(f"  {start:%d %b %H:%M}   venue unavailable")
                continue
            ratio = lake / venue
            state = classify(ratio)
            if state != "ok":
                findings.append((symbol, start, ratio, state))
            print(f"  {start:%d %b %H:%M}{lake:>15,.1f}{venue:>15,.1f}"
                  f"{ratio:>8.3f}  {state}")
    print()
    if findings:
        for symbol, start, ratio, state in findings:
            print(f"  !! {symbol} {start:%d %b %H:%M} ratio {ratio:.3f} -> {state}")
    else:
        print("  all audited hours healthy")


if __name__ == "__main__":
    main()
