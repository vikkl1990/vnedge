"""Two-speed book replay: fast coil sleeve + slow dip/reclaim sleeves.

RESEARCH_ONLY evidence tool for the uplift doctrine (2026-08-19): the fast
compression scanner keeps its identity, and two *slow* sleeves are added so
the book is present when returns cluster into a few hours.

Sleeves (all on closed 1h bars, all long/short symmetric):

- S2 flush_reclaim : a new N-hour low on >= flush_vol_mult volume that closes
  back above the prior low (the breakdown failed).  Enters into weakness,
  which the 48h field test ranked above every buy-after-strength entry.
- S3 pullback_trend: in an EMA-slope trend, price touches the EMA and closes
  back on the trend side.  Buys the dip inside the trend.

Both use a WIDE stop (beyond the setup extreme plus an ATR pad), no instant
breakeven, a trail that only arms after +1.5R, and a multi-day time stop --
deliberately the opposite of the scalper's geometry, because their job is
time in market rather than selectivity.

Regime router: VR = ATR(short) / ATR(long).  EXPAND when VR is elevated or
the 24h drift is decisive; RANGE otherwise; STRESS on a volatility blowout.
The router is reported both ON and OFF so its contribution is measurable
rather than assumed.

Costs: Delta all-in taker, 5.9 bps per leg.  The Scalper Offer is NOT applied
to the slow sleeves -- they are expected to hold well past 30 minutes.

Usage:
    python -m research.uplift_book_replay --days 30
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import urllib.request

UTC = dt.timezone.utc
TAKER_BPS = 5.9
ROUND_TRIP = 2 * TAKER_BPS

# --- S2 flush reclaim -------------------------------------------------------
FLUSH_LOOKBACK = 12
FLUSH_VOL_MULT = 2.0
FLUSH_STOP_ATR_PAD = 0.5
# --- S3 pullback in trend ---------------------------------------------------
TREND_EMA = 20
TREND_SLOPE_BARS = 6
PULLBACK_STOP_ATR_PAD = 0.5
# --- shared slow-sleeve exit ------------------------------------------------
TRAIL_ARM_R = 1.5
TRAIL_ATR_MULT = 2.0
MAX_HOLD_BARS = 48
ATR_PERIOD = 14
# --- regime router ----------------------------------------------------------
VR_SHORT = 6
VR_LONG = 48
VR_EXPAND = 1.10
VR_STRESS = 2.50
DRIFT_BPS_EXPAND = 80.0


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


def _atr(bars: list[tuple], i: int, period: int = ATR_PERIOD) -> float:
    lo = max(1, i - period)
    return statistics.mean(
        max(
            bars[j][2] - bars[j][3],
            abs(bars[j][2] - bars[j - 1][4]),
            abs(bars[j][3] - bars[j - 1][4]),
        )
        for j in range(lo, i)
    )


def _ema(values: list[float], span: int) -> list[float]:
    k = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def regime_at(bars: list[tuple], i: int) -> str:
    """EXPAND / RANGE / STRESS from a causal volatility ratio + drift."""
    if i < VR_LONG + 2:
        return "RANGE"
    short = _atr(bars, i, VR_SHORT)
    long = _atr(bars, i, VR_LONG)
    if long <= 0:
        return "RANGE"
    vr = short / long
    if vr >= VR_STRESS:
        return "STRESS"
    drift = abs(bars[i - 1][4] / bars[i - 25][4] - 1) * 1e4 if i >= 26 else 0.0
    if vr >= VR_EXPAND or drift >= DRIFT_BPS_EXPAND:
        return "EXPAND"
    return "RANGE"


def _slow_exit(bars: list[tuple], i: int, side: str, entry: float, stop: float,
               risk: float) -> tuple[float, str, int]:
    """Shared slow-sleeve management: wide stop, trail only after +1.5R."""
    extreme = entry
    for j in range(i + 1, min(i + MAX_HOLD_BARS + 1, len(bars))):
        b = bars[j]
        extreme = max(extreme, b[2]) if side == "long" else min(extreme, b[3])
        mfe = (extreme - entry) if side == "long" else (entry - extreme)
        hit = b[3] <= stop if side == "long" else b[2] >= stop
        if hit:
            gross = ((stop / entry - 1) if side == "long" else (1 - stop / entry)) * 1e4
            return gross - ROUND_TRIP, "stop", j - i
        if mfe >= TRAIL_ARM_R * risk:
            atr = _atr(bars, j)
            trail = (
                extreme - TRAIL_ATR_MULT * atr
                if side == "long"
                else extreme + TRAIL_ATR_MULT * atr
            )
            stop = max(stop, trail) if side == "long" else min(stop, trail)
    last = bars[min(i + MAX_HOLD_BARS, len(bars) - 1)]
    gross = ((last[4] / entry - 1) if side == "long" else (1 - last[4] / entry)) * 1e4
    return gross - ROUND_TRIP, "time", min(MAX_HOLD_BARS, len(bars) - 1 - i)


def flush_reclaim(bars: list[tuple], start_i: int, *, router: bool) -> list[dict]:
    """S2: failed breakdown -- new N-hour low on volume, closing back inside."""
    trades: list[dict] = []
    busy_until = -1
    for i in range(max(start_i, FLUSH_LOOKBACK + VR_LONG + 2), len(bars) - 1):
        if i <= busy_until:
            continue
        if router and regime_at(bars, i) == "STRESS":
            continue
        window = bars[i - FLUSH_LOOKBACK : i]
        prior_low = min(b[3] for b in window)
        prior_high = max(b[2] for b in window)
        vol_ma = statistics.mean(b[5] for b in bars[i - 24 : i]) if i >= 24 else bars[i][5]
        b = bars[i]
        atr = _atr(bars, i)
        if atr <= 0 or vol_ma <= 0:
            continue
        long_setup = b[3] < prior_low and b[4] > prior_low and b[5] >= FLUSH_VOL_MULT * vol_ma
        short_setup = b[2] > prior_high and b[4] < prior_high and b[5] >= FLUSH_VOL_MULT * vol_ma
        if not (long_setup or short_setup):
            continue
        side = "long" if long_setup else "short"
        entry = b[4]
        stop = (
            b[3] - FLUSH_STOP_ATR_PAD * atr
            if side == "long"
            else b[2] + FLUSH_STOP_ATR_PAD * atr
        )
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        net, reason, held = _slow_exit(bars, i, side, entry, stop, risk)
        trades.append(
            {"sleeve": "S2_flush_reclaim", "ts": b[0], "side": side,
             "net_bps": net, "reason": reason, "held": held}
        )
        busy_until = i + held
    return trades


def pullback_trend(bars: list[tuple], start_i: int, *, router: bool) -> list[dict]:
    """S3: in an EMA-slope trend, buy the touch that closes back with trend."""
    closes = [b[4] for b in bars]
    ema = _ema(closes, TREND_EMA)
    trades: list[dict] = []
    busy_until = -1
    for i in range(max(start_i, TREND_EMA + VR_LONG + 2), len(bars) - 1):
        if i <= busy_until:
            continue
        if router and regime_at(bars, i) != "EXPAND":
            continue
        b = bars[i]
        atr = _atr(bars, i)
        if atr <= 0 or i < TREND_SLOPE_BARS + 1:
            continue
        up = ema[i] > ema[i - TREND_SLOPE_BARS]
        down = ema[i] < ema[i - TREND_SLOPE_BARS]
        long_setup = up and b[3] <= ema[i] and b[4] > ema[i]
        short_setup = down and b[2] >= ema[i] and b[4] < ema[i]
        if not (long_setup or short_setup):
            continue
        side = "long" if long_setup else "short"
        entry = b[4]
        stop = (
            b[3] - PULLBACK_STOP_ATR_PAD * atr
            if side == "long"
            else b[2] + PULLBACK_STOP_ATR_PAD * atr
        )
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        net, reason, held = _slow_exit(bars, i, side, entry, stop, risk)
        trades.append(
            {"sleeve": "S3_pullback_trend", "ts": b[0], "side": side,
             "net_bps": net, "reason": reason, "held": held}
        )
        busy_until = i + held
    return trades


def summarize(trades: list[dict], label: str, notional: float) -> dict:
    if not trades:
        return {"label": label, "n": 0, "wins": 0, "pf": 0.0, "net_bps": 0.0,
                "net_usd": 0.0, "hours": 0}
    wins = [t for t in trades if t["net_bps"] > 0]
    gross_win = sum(t["net_bps"] for t in wins)
    gross_loss = -sum(t["net_bps"] for t in trades if t["net_bps"] <= 0)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    net = sum(t["net_bps"] for t in trades)
    return {
        "label": label, "n": len(trades), "wins": len(wins), "pf": pf,
        "net_bps": net, "net_usd": net * notional / 1e4,
        "hours": sum(t["held"] for t in trades),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--notional", type=float, default=3000.0)
    parser.add_argument("--router", action="store_true", help="apply the regime router")
    args = parser.parse_args()

    now = int(dt.datetime.now(UTC).timestamp() * 1000)
    end = now - now % 3_600_000
    windows = [("24h", 1), ("48h", 2), ("7d", 7), ("30d", 30)]
    max_days = max(args.days, max(d for _, d in windows))

    tapes: dict[str, list[tuple]] = {}
    for symbol in args.symbols.split(","):
        s = symbol.strip()
        tapes[s] = fetch(s, "1h", end - (max_days + 12) * 86_400_000, end)

    for label, days in windows:
        start = end - days * 86_400_000
        rows: list[dict] = []
        bench = 0.0
        for symbol, bars in tapes.items():
            start_i = next((i for i, b in enumerate(bars) if b[0] >= start), len(bars))
            if start_i >= len(bars) - 2:
                continue
            rows += flush_reclaim(bars, start_i, router=args.router)
            rows += pullback_trend(bars, start_i, router=args.router)
            bench += (bars[-1][4] / bars[start_i][1] - 1) * 1e4 - ROUND_TRIP
        s2 = summarize([t for t in rows if t["sleeve"].startswith("S2")], "S2 flush", args.notional)
        s3 = summarize([t for t in rows if t["sleeve"].startswith("S3")], "S3 pullback", args.notional)
        book = summarize(rows, "slow book", args.notional)
        hours = days * 24 * len(tapes)
        print(
            f"\n== {label} (router={'on' if args.router else 'off'})"
            f"  |  buy&hold benchmark {bench:+.0f}bps"
        )
        print(f"   {'sleeve':<14}{'n':>4}{'wins':>6}{'PF':>7}{'net bps':>10}{'net $':>10}{'time in mkt':>13}")
        for s in (s2, s3, book):
            pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
            presence = f"{100 * s['hours'] / hours:.0f}%" if hours else "-"
            print(
                f"   {s['label']:<14}{s['n']:>4}{s['wins']:>6}{pf:>7}"
                f"{s['net_bps']:>+10.0f}{s['net_usd']:>+10.2f}{presence:>13}"
            )


if __name__ == "__main__":
    main()
