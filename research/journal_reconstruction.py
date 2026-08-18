"""Reconstruct the shadow journal under the current engine plane.

RESEARCH_ONLY.  The live decision journal is append-only and is never
rewritten: this tool produces a *parallel* reconstruction showing what
``squeeze_expansion_breakout_v2`` + TriggerEngine + ExitEngine would have
journaled over the same window the fee-wall observer covered, so the two
planes can be compared record for record.

Nothing here writes to the live journal, submits an order, or grants
capital permission.  Output is a report plus optional JSONL for review.

Usage:
    python -m research.journal_reconstruction --from 2026-08-17 --to 2026-08-19
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from research.squeeze_trigger_replay import fetch
from vnedge.runtime.squeeze_observe import SqueezeObserveRunner
from vnedge.strategy.squeeze_expansion_breakout import PARAMS, SqueezeExpansionBreakout

UTC = dt.timezone.utc
WARMUP_BARS = PARAMS.rank_lookback_bars + PARAMS.compression_bars + 2


class _CollectingJournal:
    """Journal-shaped sink that keeps records in memory."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, kind: str, payload: dict) -> None:
        self.records.append({"kind": kind, "payload": payload})


def reconstruct(symbol: str, start: dt.datetime, end: dt.datetime,
                notional: float) -> list[dict]:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    warm_ms = start_ms - (WARMUP_BARS + 64) * 300_000
    bars = fetch(symbol, "5m", warm_ms, end_ms)
    if len(bars) <= WARMUP_BARS:
        return []
    frame = pd.DataFrame(
        {
            "open": [b[1] for b in bars],
            "high": [b[2] for b in bars],
            "low": [b[3] for b in bars],
            "close": [b[4] for b in bars],
            "volume": [b[5] for b in bars],
        }
    )
    prepared = SqueezeExpansionBreakout().prepare(frame)
    journal = _CollectingJournal()
    runner = SqueezeObserveRunner(journal=journal, symbol=symbol, notional_usd=notional)
    for i in range(len(prepared)):
        ts = dt.datetime.fromtimestamp(bars[i][0] / 1000, UTC)
        if bars[i][0] < start_ms:
            # still stream the bar so engine state is warm, but suppress arming
            # by only driving the runner once inside the reporting window
            continue
        runner.on_prepared_bar(prepared, i, ts)
    for record in journal.records:
        record["payload"]["symbol"] = symbol
    return journal.records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", default="2026-08-17")
    parser.add_argument("--to", dest="end", default="2026-08-19")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--notional", type=float, default=3000.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    start = dt.datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = dt.datetime.fromisoformat(args.end).replace(tzinfo=UTC)

    all_records: list[dict] = []
    for symbol in args.symbols.split(","):
        all_records += reconstruct(symbol.strip(), start, end, args.notional)

    intents = [r for r in all_records if r["kind"] == "shadow_intent"]
    outcomes = [r for r in all_records if r["kind"] == "shadow_outcome"]
    print(f"\nRECONSTRUCTED JOURNAL  {start:%d %b %H:%M} -> {end:%d %b %H:%M} UTC")
    print(f"  strategy : squeeze_expansion_breakout_v2 (trigger + exit plane)")
    print(f"  records  : {len(intents)} shadow_intent, {len(outcomes)} shadow_outcome\n")
    if not outcomes:
        print("  no fires in window")
        return
    net = 0.0
    print(f"  {'entry (UTC)':<17}{'sym':<5}{'side':<6}{'resolution':<17}"
          f"{'bars':>5}{'net USD':>10}")
    for record in outcomes:
        payload = record["payload"]
        stamp = payload.get("entry_bar_ts") or payload["bar_ts"]
        bar_ts = dt.datetime.fromisoformat(stamp)
        net += float(payload["virtual_net_usd"])
        print(
            f"  {bar_ts:%d %b %H:%M}    {payload['symbol'][:3]:<5}"
            f"{payload['side']:<6}{payload['resolution']:<17}"
            f"{payload['bars_held']:>5}{float(payload['virtual_net_usd']):>+10.2f}"
        )
    wins = sum(1 for r in outcomes if float(r["payload"]["virtual_net_usd"]) > 0)
    print(f"\n  closed {len(outcomes)}  wins {wins}  net ${net:+.2f}")

    if args.out:
        args.out.write_text("\n".join(json.dumps(r, default=str) for r in all_records))
        print(f"  written -> {args.out}")


if __name__ == "__main__":
    main()
