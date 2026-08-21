"""Exploratory replay for squeeze_expansion_breakout_v3.

This is NOT tick-parity evidence. Public Binance 1-minute closes stand in for
executable quote observations, so the frozen live rule (3 samples held for 5s)
becomes a deliberately slower three-minute confirmation. Closed 5-minute bars
still own compression features and the complete ExitEngine policy. The exact
v3 acceptance state object and full conservative Delta cost profile are used.

Use this to reject obvious lifecycle/economics failures and to compare whether
v3 leaves a fingerprint on a large move. Promotion evidence requires recorded
bid/ask ticks from the VNEDGE tick lake.

Usage:
    python -m research.squeeze_acceptance_replay --days 4 \
        --symbols BTCUSDT,ETHUSDT
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import urllib.request

import pandas as pd

from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.runtime.expansion_acceptance import CompressionArm, ExpansionAcceptanceEngine
from vnedge.runtime.scanner_session import SessionCosts
from vnedge.strategy.squeeze_expansion_breakout_v3 import (
    PARAMS,
    SqueezeExpansionBreakoutV3,
)

UTC = dt.UTC


def fetch_1m(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    rows: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (
            "https://fapi.binance.com/fapi/v1/klines"
            f"?symbol={symbol}&interval=1m&startTime={cursor}"
            f"&endTime={end_ms}&limit=1500"
        )
        with urllib.request.urlopen(url, timeout=30) as response:
            batch = json.load(response)
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 60_000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
    frame = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(int(row[0]), unit="ms", tz="UTC"),
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]),
            }
            for row in rows
        ]
    )
    return frame.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def to_5m(one: pd.DataFrame) -> pd.DataFrame:
    frame = one.set_index("timestamp")
    out = frame.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna().reset_index()
    return out


def replay(symbol: str, one: pd.DataFrame, eval_start: pd.Timestamp) -> list[dict]:
    five = SqueezeExpansionBreakoutV3().prepare(to_5m(one)).reset_index(drop=True)
    engine = ExpansionAcceptanceEngine()
    exits = ExitEngine(ExitConfig(
        breakeven_cost_bps=PARAMS.round_trip_cost_bps,
        be_fee_buffer_bps=PARAMS.breakeven_buffer_bps,
    ))
    costs = SessionCosts.from_profile("delta_scalp")
    trades: list[dict] = []
    open_meta: dict | None = None
    five_index = -1

    for minute in one.itertuples(index=False):
        quote_ts = minute.timestamp + pd.Timedelta(minutes=1)
        # Process every 5m candle proven closed by this minute timestamp.
        while (
            five_index + 1 < len(five)
            and five.iloc[five_index + 1]["timestamp"] + pd.Timedelta(minutes=5)
            <= quote_ts
        ):
            five_index += 1
            row = five.iloc[five_index]
            if open_meta is not None:
                decision = exits.on_bar(
                    high=float(row["high"]), low=float(row["low"]),
                    close=float(row["close"]),
                    atr=float(row["sqz_atr"]) if math.isfinite(float(row["sqz_atr"])) else 0.0,
                    bar_index=five_index,
                )
                if decision is not None:
                    side = open_meta["side"]
                    entry = open_meta["entry"]
                    gross = (
                        (decision.price / entry - 1)
                        if side == "long" else (1 - decision.price / entry)
                    ) * 10_000
                    held = five_index - open_meta["entry_bar"]
                    net = gross - costs.round_trip_bps(held)
                    trades.append({
                        "symbol": symbol, "side": side,
                        "entry_ts": open_meta["entry_ts"],
                        "exit_ts": row["timestamp"], "entry": entry,
                        "exit": decision.price, "reason": decision.reason,
                        "held_bars": held, "gross_bps": gross, "net_bps": net,
                    })
                    engine.notify_flat(bar_index=five_index, net_won=net > 0)
                    open_meta = None
            values = [
                float(row[name]) for name in (
                    "sqz_range_high", "sqz_range_low", "sqz_atr", "sqz_vwap24",
                    "sqz_episode", "sqz_compressed",
                )
            ]
            if all(math.isfinite(value) for value in values):
                engine.update_arm(CompressionArm(
                    episode_id=int(row["sqz_episode"]),
                    box_high=float(row["sqz_range_high"]),
                    box_low=float(row["sqz_range_low"]),
                    atr=float(row["sqz_atr"]), vwap=float(row["sqz_vwap24"]),
                    bar_index=five_index,
                    compressed=float(row["sqz_compressed"]) > 0,
                ))

        if quote_ts < eval_start or five_index < 0:
            continue
        price = float(minute.close)
        if open_meta is not None:
            pos = exits.pos
            tick_exit = exits.on_tick(price=price)
            if tick_exit is not None and pos is not None:
                side = open_meta["side"]
                entry = open_meta["entry"]
                gross = (
                    (tick_exit.price / entry - 1)
                    if side == "long" else (1 - tick_exit.price / entry)
                ) * 10_000
                held = five_index - open_meta["entry_bar"]
                net = gross - costs.round_trip_bps(held)
                trades.append({
                    "symbol": symbol, "side": side,
                    "entry_ts": open_meta["entry_ts"], "exit_ts": quote_ts,
                    "entry": entry, "exit": tick_exit.price,
                    "reason": tick_exit.reason, "held_bars": held,
                    "gross_bps": gross, "net_bps": net,
                })
                engine.notify_flat(bar_index=five_index, net_won=net > 0)
                open_meta = None
            continue
        fire = engine.observe_quote(
            bid=price, ask=price, ts=quote_ts.to_pydatetime(), bar_index=five_index
        )
        if fire is not None:
            exits.open_from_fire(
                side=fire.side, entry=fire.entry, stop=fire.stop, risk=fire.risk,
                box_edge=fire.box_edge, entry_bar=five_index,
            )
            open_meta = {
                "side": fire.side, "entry": fire.entry,
                "entry_bar": five_index, "entry_ts": quote_ts,
            }
    return trades


def report(trades: list[dict], *, days: int, notional: float) -> None:
    wins = [trade for trade in trades if trade["net_bps"] > 0]
    gross_wins = sum(trade["net_bps"] for trade in wins)
    gross_losses = -sum(trade["net_bps"] for trade in trades if trade["net_bps"] <= 0)
    net = sum(trade["net_bps"] for trade in trades)
    pf = gross_wins / gross_losses if gross_losses else float("inf")
    print("v3 exploratory 1m acceptance proxy (NOT tick-parity evidence)")
    print(
        f"window={days}d trades={len(trades)} wins={len(wins)} "
        f"PF={pf:.3f} net_bps={net:.1f} net_usd={net * notional / 10_000:.2f}"
    )
    for symbol in sorted({trade["symbol"] for trade in trades}):
        rows = [trade for trade in trades if trade["symbol"] == symbol]
        print(
            f"{symbol}: trades={len(rows)} wins={sum(t['net_bps'] > 0 for t in rows)} "
            f"net_bps={sum(t['net_bps'] for t in rows):.1f}"
        )
    reasons: dict[str, int] = {}
    for trade in trades:
        reasons[trade["reason"]] = reasons.get(trade["reason"], 0) + 1
    print("exits=" + json.dumps(reasons, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=4)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--notional", type=float, default=3000.0)
    args = parser.parse_args()
    now = dt.datetime.now(UTC).replace(second=0, microsecond=0)
    # Seven-day rank + compression window + evaluation window + margin.
    warmup_days = 9
    start = now - dt.timedelta(days=args.days + warmup_days)
    eval_start = pd.Timestamp(now - dt.timedelta(days=args.days))
    trades: list[dict] = []
    for symbol in [item.strip().upper() for item in args.symbols.split(",") if item.strip()]:
        one = fetch_1m(symbol, int(start.timestamp() * 1000), int(now.timestamp() * 1000))
        if one.empty:
            print(f"{symbol}: no public rows")
            continue
        trades.extend(replay(symbol, one, eval_start))
    report(trades, days=args.days, notional=args.notional)


if __name__ == "__main__":
    main()
