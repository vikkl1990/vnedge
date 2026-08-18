"""Unified scanner report: every arm, every window, one shared session.

Replaces the per-hypothesis replay scripts.  Each of those reimplemented
the same bar loop; they now share ``ScannerSession`` so a number printed
here is produced by the identical code the shadow lane runs.

Also reconstructs the shadow journal for a window: the records the lane
WOULD have written, for comparison against the live append-only journal
(which this tool never modifies).

Usage:
    python -m research.scanner_report --days 90
    python -m research.scanner_report --journal --from 2026-08-17 --to 2026-08-19
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
from pathlib import Path

from research.squeeze_trigger_replay import fetch
from vnedge.runtime.scanner_session import ScannerSession, summarize
from vnedge.strategy.arm_sources import (
    CoilArmSource,
    CompositeArmSource,
    IgnitionArmSource,
)

UTC = dt.timezone.utc
WARMUP_DAYS = 9


def build_arm(mode: str):
    if mode == "coil":
        return CoilArmSource()
    if mode == "ignition":
        return IgnitionArmSource()
    if mode == "hybrid":
        return CompositeArmSource(sources=[CoilArmSource(), IgnitionArmSource()])
    raise ValueError(f"unknown arm mode: {mode}")


def load(symbol: str, start_ms: int, end_ms: int, attempts: int = 4) -> list[tuple]:
    for attempt in range(attempts):
        try:
            return fetch(symbol, "5m", start_ms, end_ms)
        except urllib.error.HTTPError:
            time.sleep(6 * (attempt + 1))
    raise RuntimeError(f"could not load {symbol} (venue throttled)")


def run_window(tapes: dict[str, list[tuple]], mode: str, start_ms: int) -> list:
    trades = []
    for symbol, bars in tapes.items():
        session = ScannerSession(symbol=symbol, arm_source=build_arm(mode))
        trades += session.run(bars, start_ms=start_ms)
    return trades


def report(args: argparse.Namespace, tapes: dict[str, list[tuple]], end_ms: int) -> None:
    windows = [("24h", 1), ("48h", 2), ("7d", 7), ("30d", 30), (f"{args.days}d", args.days)]
    seen: set[int] = set()
    for label, days in windows:
        if days in seen or days > args.days:
            continue
        seen.add(days)
        start = end_ms - days * 86_400_000
        print(f"\n== {label}")
        print(f"   {'arm':<10}{'n':>5}{'wins':>6}{'PF':>7}{'net bps':>10}{'net $':>10}")
        for mode in args.modes.split(","):
            trades = run_window(tapes, mode.strip(), start)
            s = summarize(trades, args.notional)
            pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
            print(
                f"   {mode.strip():<10}{s['n']:>5}{s['wins']:>6}{pf:>7}"
                f"{s['net_bps']:>+10.0f}{s['net_usd']:>+10.2f}"
            )


def journal(args: argparse.Namespace, tapes: dict[str, list[tuple]]) -> None:
    start = dt.datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = dt.datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    records: list[dict] = []

    for symbol, bars in tapes.items():
        session = ScannerSession(symbol=symbol, arm_source=build_arm(args.arm))
        for trade in session.run(bars, start_ms=start_ms):
            if trade.exit_ts_ms > int(end.timestamp() * 1000):
                continue
            key = (
                f"{args.arm}|{symbol}|{trade.side}|{trade.entry_ts_ms}"
            )
            records.append({
                "kind": "shadow_intent",
                "payload": {
                    "intent_key": key, "approved": True, "symbol": symbol,
                    "side": trade.side, "arm": trade.arm,
                    "strategy_id": "squeeze_expansion_breakout_v2",
                    "chase_bps": round(trade.chase_bps, 2),
                    "bar_ts": trade.entry_time.isoformat(),
                },
            })
            records.append({
                "kind": "shadow_outcome",
                "payload": {
                    "intent_key": key, "symbol": symbol, "side": trade.side,
                    "arm": trade.arm, "resolution": trade.reason,
                    "entry_bar_ts": trade.entry_time.isoformat(),
                    "bar_ts": trade.exit_time.isoformat(),
                    "entry_price": round(trade.entry_price, 4),
                    "exit_price": round(trade.exit_price, 4),
                    "bars_held": trade.held_bars,
                    "captured_bps": round(trade.gross_bps, 2),
                    "fees_bps": round(trade.fee_bps, 2),
                    "virtual_net_usd": round(trade.net_bps * args.notional / 1e4, 4),
                },
            })

    outcomes = [r for r in records if r["kind"] == "shadow_outcome"]
    print(f"\nRECONSTRUCTED JOURNAL  arm={args.arm}  "
          f"{start:%d %b %H:%M} -> {end:%d %b %H:%M} UTC")
    print(f"  records: {len(records) // 2} intents / {len(outcomes)} outcomes  "
          "(live journal untouched)\n")
    if not outcomes:
        print("  no fires in window")
        return
    print(f"  {'entry (UTC)':<15}{'sym':<5}{'arm':<10}{'side':<6}"
          f"{'resolution':<17}{'bars':>5}{'chase':>7}{'net $':>9}")
    net = 0.0
    for record in outcomes:
        p = record["payload"]
        chase = next(
            (i["payload"]["chase_bps"] for i in records
             if i["kind"] == "shadow_intent" and i["payload"]["intent_key"] == p["intent_key"]),
            0.0,
        )
        net += p["virtual_net_usd"]
        stamp = dt.datetime.fromisoformat(p["entry_bar_ts"])
        print(
            f"  {stamp:%d %b %H:%M}  {p['symbol'][:3]:<5}{p['arm']:<10}{p['side']:<6}"
            f"{p['resolution']:<17}{p['bars_held']:>5}{chase:>7.1f}"
            f"{p['virtual_net_usd']:>+9.2f}"
        )
    wins = sum(1 for r in outcomes if r["payload"]["virtual_net_usd"] > 0)
    print(f"\n  closed {len(outcomes)}  wins {wins}  net ${net:+.2f}")
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r) for r in records))
        print(f"  written -> {args.out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--modes", default="coil,ignition,hybrid")
    parser.add_argument("--notional", type=float, default=3000.0)
    parser.add_argument("--journal", action="store_true")
    parser.add_argument("--arm", default="hybrid")
    parser.add_argument("--from", dest="start", default="2026-08-17")
    parser.add_argument("--to", dest="end", default="2026-08-19")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    now = int(dt.datetime.now(UTC).timestamp() * 1000)
    end_ms = now - now % 300_000
    span_days = args.days if not args.journal else 30
    warm_ms = end_ms - (span_days + WARMUP_DAYS) * 86_400_000
    tapes = {
        s.strip(): load(s.strip(), warm_ms, end_ms) for s in args.symbols.split(",")
    }
    if args.journal:
        journal(args, tapes)
    else:
        report(args, tapes, end_ms)


if __name__ == "__main__":
    main()
