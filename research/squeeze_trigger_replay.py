"""Reference replay of the squeeze_expansion_breakout_v2 trigger/exit planes.

RESEARCH_ONLY evidence tool.  Replays the full reviewed design over public
Binance 5m bars (as a Delta price proxy):

- ARM  (scanner plane): compression-range rank <= 0.20 over ~7 days, computed
  on prior bars exactly like the registered strategy; armed levels are the
  compression box edges plus a buffer; one fire per compression episode.
- S4   (bias veto): fires only on the side of the rolling 24h VWAP.
- FIRE (trigger plane): intrabar stop-through of the armed level with a
  max-chase cap measured from the level (bar close beyond level + cap
  cancels the arm as "move already gone"), volume confirmation on the fire
  bar, one net position per symbol, <= 4 fires per UTC day, >= 90 minutes
  between fires, cooldown 45m after a loss / 20m after a win.
- EXIT (exit plane): hard SL anchored at the ARM level minus 1.7 * ATR(48)
  (never re-anchored to the fill), failed-breakout kill when a later bar
  closes back inside the box, no-progress time stop (MFE < 0.5R after 20m),
  breakeven-plus-fees lock after +1R, chandelier trail (extreme - 1 ATR)
  after +2R, absolute 4h backstop.  No fixed take-profit: expansion capture
  keeps the right tail.
- COST: the canonical conservative Delta profile: both tariff legs, GST,
  slippage, and the safety buffer. No Scalper/DETO discount is assumed.

Pessimistic conventions: stop checked before favorable exits inside a bar;
entries pay 1 bp through the level.  This is smoke/reference evidence on seen
data -- never a judgment; the sealed-tail prereg still governs promotion.

Usage:
    python -m research.squeeze_trigger_replay --days 97 --symbols BTCUSDT,ETHUSDT
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import statistics
import urllib.request

UTC = dt.UTC

# Frozen to the registered strategy params (strategy/squeeze_expansion_breakout.py)
COMPRESSION_BARS = 48
RANK_LOOKBACK = 2016
COMPRESSION_THRESHOLD = 0.20
VOL_LOOKBACK = 48
VOL_MULT = 1.3
ATR_PERIOD = 48
ATR_STOP_MULT = 1.7
BREAK_BUFFER_BPS = 2.0
MIN_BARS_BETWEEN_FIRES = 18          # 90 min of 5m bars

# Trigger/exit plane additions (reviewed spec, 2026-08-18)
MAX_CHASE_BPS = 20.0                 # <= 1/3 of typical stop distance
ENTRY_SLIP_BPS = 1.0
MAX_FIRES_PER_DAY = 4
COOLDOWN_LOSS_BARS = 9               # 45 min
COOLDOWN_WIN_BARS = 4                # 20 min
NO_PROGRESS_BARS = 4                 # 20 min
NO_PROGRESS_MIN_R = 0.5
BREAKEVEN_ARM_R = 1.0
TRAIL_ARM_R = 2.0
TRAIL_ATR_MULT = 1.0
ABSOLUTE_MAX_BARS = 48               # 4 h
TAKER_BPS = 5.9
SCALPER_FREE_CLOSE_BARS = 0          # compatibility export; unverified => disabled
VWAP_BARS = 288                      # 24 h of 5m
CONFIRM_CLOSE = True                 # bar-close confirmation beyond the level
                                     # (the tick plane's 3-10s hold, at bar scale)


#: Bar length in ms, needed to know when a kline has actually closed.
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


def fetch(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[tuple]:
    """Closed klines only.

    The venue returns the CURRENTLY FORMING bar as the last row whenever
    ``end_ms`` reaches into the present, and its close/high/low are not final.
    Handing that to a scanner breaks the same closed-bar discipline the live
    feed enforces ("the next interval's first update proves the previous one
    closed"), so it is dropped here rather than in each caller.
    """
    out: list[tuple] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (
            "https://fapi.binance.com/fapi/v1/klines"
            f"?symbol={symbol}&interval={interval}&startTime={cursor}"
            f"&endTime={end_ms}&limit=1000"
        )
        rows = json.load(urllib.request.urlopen(url, timeout=30))
        if not rows:
            break
        out += [
            (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            for r in rows
        ]
        nxt = rows[-1][0] + 1
        if nxt <= cursor:
            break
        cursor = nxt
    step = _INTERVAL_MS.get(interval)
    if step:
        now_ms = int(time.time() * 1000)
        out = [row for row in out if row[0] + step <= now_ms]
    return out


def replay(symbol: str, bars: list[tuple], eval_start_ms: int) -> list[dict]:
    from vnedge.execution.exit_engine import ExitConfig, ExitEngine
    from vnedge.execution.trigger_engine import ArmState, TriggerConfig, TriggerEngine
    from vnedge.runtime.scanner_session import SessionCosts

    costs = SessionCosts.from_profile("delta_scalp")

    n = len(bars)
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]

    trigger = TriggerEngine(
        config=TriggerConfig(
            max_chase_bps=MAX_CHASE_BPS,
            entry_slip_bps=ENTRY_SLIP_BPS,
            break_buffer_bps=BREAK_BUFFER_BPS,
            max_fires_per_day=MAX_FIRES_PER_DAY,
            min_bars_between_fires=MIN_BARS_BETWEEN_FIRES,
            cooldown_loss_bars=COOLDOWN_LOSS_BARS,
            cooldown_win_bars=COOLDOWN_WIN_BARS,
            confirm_close=CONFIRM_CLOSE,
            atr_stop_mult=ATR_STOP_MULT,
            vol_mult=VOL_MULT,
        )
    )
    exits = ExitEngine(
        config=ExitConfig(
            no_progress_bars=NO_PROGRESS_BARS,
            no_progress_min_r=NO_PROGRESS_MIN_R,
            breakeven_arm_r=BREAKEVEN_ARM_R,
            trail_arm_r=TRAIL_ARM_R,
            trail_atr_mult=TRAIL_ATR_MULT,
            absolute_max_bars=ABSOLUTE_MAX_BARS,
            taker_bps=TAKER_BPS,
        )
    )

    trades: list[dict] = []
    open_meta: dict | None = None
    episode_id = 0
    prev_compressed = False
    rank_window: list[float] = []
    pv_sum = v_sum = 0.0

    for i in range(n):
        ts = bars[i][0]

        # ---- rolling 24h VWAP over prior bars (bias veto) ----
        if i >= 1:
            j = i - 1
            pv_sum += closes[j] * vols[j]
            v_sum += vols[j]
            if i - 1 >= VWAP_BARS:
                k = i - 1 - VWAP_BARS
                pv_sum -= closes[k] * vols[k]
                v_sum -= vols[k]
        vwap = pv_sum / v_sum if v_sum > 0 else None

        # ---- compression state from prior bars ----
        if i < COMPRESSION_BARS + 1:
            continue
        box_high = max(highs[i - COMPRESSION_BARS : i])
        box_low = min(lows[i - COMPRESSION_BARS : i])
        range_pct = (box_high - box_low) / closes[i - 1]
        rank_window.append(range_pct)
        if len(rank_window) > RANK_LOOKBACK:
            rank_window.pop(0)
        if len(rank_window) < RANK_LOOKBACK:
            continue
        rank = sum(1 for x in rank_window if x < range_pct) / len(rank_window)
        compressed = rank <= COMPRESSION_THRESHOLD
        if compressed and not prev_compressed:
            episode_id += 1
        prev_compressed = compressed

        atr = statistics.mean(
            max(
                highs[j] - lows[j],
                abs(highs[j] - closes[j - 1]),
                abs(lows[j] - closes[j - 1]),
            )
            for j in range(i - ATR_PERIOD, i)
        )
        vol_ma = statistics.mean(vols[i - VOL_LOOKBACK : i])

        # ---- exit plane ----
        if open_meta is not None:
            decision = exits.on_bar(
                high=highs[i], low=lows[i], close=closes[i], atr=atr, bar_index=i
            )
            if decision is not None:
                held = i - open_meta["entry_bar"]
                side = open_meta["side"]
                gross = (
                    (decision.price / open_meta["entry"] - 1)
                    if side == "long"
                    else (1 - decision.price / open_meta["entry"])
                ) * 1e4
                fee = costs.round_trip_bps(held)
                trades.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "entry_ts": bars[open_meta["entry_bar"]][0],
                        "exit_ts": ts,
                        "entry": open_meta["entry"],
                        "exit": decision.price,
                        "reason": decision.reason,
                        "held_bars": held,
                        "net_bps": gross - fee,
                    }
                )
                trigger.notify_flat(i, won=decision.won)
                open_meta = None
            continue

        # ---- trigger plane ----
        if ts < eval_start_ms:
            continue
        fire = trigger.try_fire(
            arm=ArmState(
                episode_id=episode_id,
                box_high=box_high,
                box_low=box_low,
                compressed=compressed,
                atr=atr,
                vol_ma=vol_ma,
                prev_close=closes[i - 1],
            ),
            high=highs[i],
            low=lows[i],
            close=closes[i],
            volume=vols[i],
            vwap=vwap,
            bar_index=i,
            bar_ts_ms=ts,
        )
        if fire is not None:
            exits.open_from_fire(
                side=fire.side,
                entry=fire.entry,
                stop=fire.stop,
                risk=fire.risk,
                box_edge=fire.box_edge,
                entry_bar=i,
            )
            open_meta = {"side": fire.side, "entry": fire.entry, "entry_bar": i}

    return trades


def report(trades: list[dict], now_ms: int, notional: float) -> None:
    windows = [("24h", 1), ("48h", 2), ("7d", 7), ("30d", 30), ("90d", 90)]
    print(f"{'window':>6} {'trades':>7} {'wins':>5} {'PF':>6} {'net bps':>9} {'net $':>9}")
    for label, days in windows:
        cut = now_ms - days * 86_400_000
        rows = [t for t in trades if t["entry_ts"] >= cut]
        if not rows:
            print(f"{label:>6} {0:>7} {'-':>5} {'-':>6} {'-':>9} {'-':>9}")
            continue
        wins = [t for t in rows if t["net_bps"] > 0]
        gw = sum(t["net_bps"] for t in wins)
        gl = -sum(t["net_bps"] for t in rows if t["net_bps"] <= 0)
        pf = gw / gl if gl > 0 else float("inf")
        net = sum(t["net_bps"] for t in rows)
        print(
            f"{label:>6} {len(rows):>7} {len(wins):>5} {pf:>6.2f} "
            f"{net:>+9.1f} {net * notional / 1e4:>+9.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=97)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--notional", type=float, default=3000.0)
    parser.add_argument("--dump-trades", action="store_true")
    args = parser.parse_args()

    now = int(dt.datetime.now(UTC).timestamp() * 1000)
    end = now - now % 300_000
    eval_start = end - args.days * 86_400_000
    warm_start = eval_start - 8 * 86_400_000  # rank + compression warmup

    all_trades: list[dict] = []
    for symbol in args.symbols.split(","):
        bars = fetch(symbol.strip(), "5m", warm_start, end)
        trades = replay(symbol.strip(), bars, eval_start)
        all_trades += trades
        print(f"\n== {symbol.strip()} ({len(trades)} trades over {args.days}d)")
        report(trades, end, args.notional)
        if args.dump_trades:
            for t in trades:
                e = dt.datetime.fromtimestamp(t["entry_ts"] / 1000, UTC)
                x = dt.datetime.fromtimestamp(t["exit_ts"] / 1000, UTC)
                print(
                    f"   {t['side']:5s} {e:%d %b %H:%M} -> {x:%H:%M} "
                    f"{t['reason']:15s} {t['net_bps']:+7.1f}bps"
                )
    print("\n== COMBINED")
    report(all_trades, end, args.notional)


if __name__ == "__main__":
    main()
