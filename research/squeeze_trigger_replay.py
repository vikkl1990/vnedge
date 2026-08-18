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
- COST: Delta all-in taker 5.9 bps per leg; Scalper Offer models the closing
  fee as zero when the hold is <= 30 minutes.

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
import statistics
import urllib.request

UTC = dt.timezone.utc

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
SCALPER_FREE_CLOSE_BARS = 6          # 30 min
VWAP_BARS = 288                      # 24 h of 5m
CONFIRM_CLOSE = True                 # bar-close confirmation beyond the level
                                     # (the tick plane's 3-10s hold, at bar scale)


def fetch(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[tuple]:
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
    return out


def replay(symbol: str, bars: list[tuple], eval_start_ms: int) -> list[dict]:
    n = len(bars)
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]

    trades: list[dict] = []
    pos: dict | None = None
    fired_episode: int | None = None
    episode_id = 0
    prev_compressed = False
    last_fire_bar = -(10**9)
    cooldown_until_bar = -(10**9)
    fires_today = 0
    today = None
    rank_window: list[float] = []

    pv_sum = v_sum = 0.0

    for i in range(n):
        ts = bars[i][0]
        day = dt.datetime.fromtimestamp(ts / 1000, UTC).date()
        if day != today:
            today = day
            fires_today = 0

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

        # ---- manage open position (exit plane) ----
        if pos is not None:
            side = pos["side"]
            held = i - pos["entry_bar"]
            favorable = (
                (highs[i] - pos["entry"]) if side == "long" else (pos["entry"] - lows[i])
            )
            pos["mfe"] = max(pos["mfe"], favorable)
            pos["ext"] = max(pos["ext"], highs[i]) if side == "long" else min(pos["ext"], lows[i])
            risk = pos["risk"]

            def _close(price: float, reason: str) -> None:
                gross = (
                    (price / pos["entry"] - 1) if side == "long" else (1 - price / pos["entry"])
                ) * 1e4
                fee = TAKER_BPS + (0.0 if held <= SCALPER_FREE_CLOSE_BARS else TAKER_BPS)
                trades.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "entry_ts": bars[pos["entry_bar"]][0],
                        "exit_ts": ts,
                        "entry": pos["entry"],
                        "exit": price,
                        "reason": reason,
                        "held_bars": held,
                        "net_bps": gross - fee,
                    }
                )

            stop = pos["stop"]
            stop_hit = lows[i] <= stop if side == "long" else highs[i] >= stop
            if stop_hit:
                _close(stop, "stop")
                cooldown_until_bar = i + COOLDOWN_LOSS_BARS
                pos = None
                continue
            back_inside = (
                closes[i] < pos["box_edge"] if side == "long" else closes[i] > pos["box_edge"]
            )
            if held >= 1 and back_inside:
                _close(closes[i], "failed_breakout")
                cooldown_until_bar = i + COOLDOWN_LOSS_BARS
                pos = None
                continue
            if held >= NO_PROGRESS_BARS and pos["mfe"] < NO_PROGRESS_MIN_R * risk:
                _close(closes[i], "no_progress")
                cooldown_until_bar = i + COOLDOWN_LOSS_BARS
                pos = None
                continue
            if held >= ABSOLUTE_MAX_BARS:
                _close(closes[i], "time_4h")
                cooldown_until_bar = i + COOLDOWN_WIN_BARS
                pos = None
                continue
            if pos["mfe"] >= BREAKEVEN_ARM_R * risk:
                be = (
                    pos["entry"] * (1 + (TAKER_BPS + 1) / 1e4)
                    if side == "long"
                    else pos["entry"] * (1 - (TAKER_BPS + 1) / 1e4)
                )
                pos["stop"] = max(pos["stop"], be) if side == "long" else min(pos["stop"], be)
            if pos["mfe"] >= TRAIL_ARM_R * risk:
                trail = (
                    pos["ext"] - TRAIL_ATR_MULT * atr
                    if side == "long"
                    else pos["ext"] + TRAIL_ATR_MULT * atr
                )
                pos["stop"] = max(pos["stop"], trail) if side == "long" else min(pos["stop"], trail)
            continue

        # ---- trigger plane ----
        if ts < eval_start_ms or not compressed or fired_episode == episode_id:
            continue
        if i < cooldown_until_bar or i - last_fire_bar < MIN_BARS_BETWEEN_FIRES:
            continue
        if fires_today >= MAX_FIRES_PER_DAY or vwap is None:
            continue
        buf = closes[i - 1] * BREAK_BUFFER_BPS / 1e4
        long_level = box_high + buf
        short_level = box_low - buf
        volume_ok = vols[i] > VOL_MULT * vol_ma
        side = None
        confirmed_long = (
            closes[i] > long_level if CONFIRM_CLOSE else highs[i] > long_level
        )
        confirmed_short = (
            closes[i] < short_level if CONFIRM_CLOSE else lows[i] < short_level
        )
        if confirmed_long and closes[i - 1] > vwap:
            side, level, box_edge = "long", long_level, box_high
        elif confirmed_short and closes[i - 1] < vwap:
            side, level, box_edge = "short", short_level, box_low
        if side is None or not volume_ok:
            continue
        chase = (
            (closes[i] - level) / level if side == "long" else (level - closes[i]) / level
        ) * 1e4
        if chase > MAX_CHASE_BPS:
            fired_episode = episode_id  # move already gone; burn the arm
            continue
        entry = (
            level * (1 + ENTRY_SLIP_BPS / 1e4)
            if side == "long"
            else level * (1 - ENTRY_SLIP_BPS / 1e4)
        )
        risk = ATR_STOP_MULT * atr
        stop = level - risk if side == "long" else level + risk
        pos = {
            "side": side,
            "entry": entry,
            "entry_bar": i,
            "stop": stop,
            "risk": risk,
            "box_edge": box_edge,
            "mfe": 0.0,
            "ext": entry,
        }
        fired_episode = episode_id
        last_fire_bar = i
        fires_today += 1

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
