"""Market movement vs scanner coverage: what moved, what we saw, what we missed.

RESEARCH_ONLY.  Answers three questions per window, per symbol:

1. WHAT MOVED -- net, range, how concentrated the return was in a few hours,
   persistence, session split, and the count of "expansion events" (a move of
   at least ``event_bps`` completed within ``event_hours``, measured forward
   from a bar and requiring the move to run before an adverse ``event_bps/2``).
2. WHAT WE DETECTED -- for every event, whether an arm existed near it and
   whether a position was actually open while it ran.
3. WHERE THE EDGE LEAKS -- each event is attributed to exactly one cause:
   CAUGHT, BLIND (no arm existed), GATED (armed but the trigger refused),
   or EARLY (we were in and exited before the move completed).

The attribution is deliberately unforgiving: an event counts as caught only
if a position was open at the moment the move actually ran.

Usage:
    python -m research.coverage_analysis --symbols BTCUSDT,ETHUSDT
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import time
import urllib.error
from dataclasses import dataclass

from research.squeeze_trigger_replay import fetch
from vnedge.runtime.scanner_session import ScannerSession, ScannerTrade, summarize
from vnedge.strategy.arm_sources import (
    BarContext,
    CoilArmSource,
    CompositeArmSource,
    IgnitionArmSource,
)

UTC = dt.timezone.utc
BARS_PER_HOUR = 12  # 5m bars

WINDOWS = (("24h", 1), ("48h", 2), ("72h", 3), ("1 week", 7), ("1 month", 30))


@dataclass(frozen=True, slots=True)
class Event:
    """A directional expansion the book would have wanted to be in."""

    index: int
    ts_ms: int
    side: str
    move_bps: float
    bars_to_complete: int

    @property
    def time(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.ts_ms / 1000, UTC)


def load(symbol: str, start_ms: int, end_ms: int, attempts: int = 5) -> list[tuple]:
    for attempt in range(attempts):
        try:
            return fetch(symbol, "5m", start_ms, end_ms)
        except urllib.error.HTTPError:
            time.sleep(6 * (attempt + 1))
    raise RuntimeError(f"{symbol}: venue throttled")


def find_events(bars: list[tuple], start_i: int, *, event_bps: float,
                event_hours: int) -> list[Event]:
    """Non-overlapping forward moves of event_bps that beat an adverse half-move."""
    horizon = event_hours * BARS_PER_HOUR
    events: list[Event] = []
    busy_until = -1
    for i in range(start_i, len(bars) - 1):
        if i <= busy_until:
            continue
        ref = bars[i][4]
        up_ok = dn_ok = True
        for j in range(i + 1, min(i + horizon + 1, len(bars))):
            b = bars[j]
            if up_ok:
                if (b[3] / ref - 1) * 1e4 <= -event_bps / 2:
                    up_ok = False
                elif (b[2] / ref - 1) * 1e4 >= event_bps:
                    events.append(Event(i, bars[i][0], "long", event_bps, j - i))
                    busy_until = j
                    break
            if dn_ok:
                if (1 - b[2] / ref) * 1e4 <= -event_bps / 2:
                    dn_ok = False
                elif (1 - b[3] / ref) * 1e4 >= event_bps:
                    events.append(Event(i, bars[i][0], "short", event_bps, j - i))
                    busy_until = j
                    break
            if not up_ok and not dn_ok:
                break
    return events


def arm_index(bars: list[tuple], start_i: int, source_factory) -> dict[int, str]:
    """Bars where an arm existed, independent of whether the trigger fired."""
    source = source_factory()
    armed: dict[int, str] = {}
    pv = vv = 0.0
    for i in range(len(bars)):
        if i >= 1:
            pv += bars[i - 1][4] * bars[i - 1][5]
            vv += bars[i - 1][5]
            if i - 1 >= 288:
                k = i - 1 - 288
                pv -= bars[k][4] * bars[k][5]
                vv -= bars[k][5]
        if i < 49:
            continue
        atr = statistics.mean(
            max(bars[j][2] - bars[j][3], abs(bars[j][2] - bars[j - 1][4]),
                abs(bars[j][3] - bars[j - 1][4]))
            for j in range(i - 48, i)
        )
        vol_ma = statistics.mean(b[5] for b in bars[i - 48 : i])
        ctx = BarContext(bars=bars, index=i, atr=atr, vol_ma=vol_ma,
                         vwap=pv / vv if vv > 0 else None, prev_close=bars[i - 1][4])
        state = source.observe(ctx)
        if state is not None and i >= start_i:
            armed[i] = getattr(source, "last_armed", None) or source.name
    return armed


def attribute(events: list[Event], armed: dict[int, str],
              trades: list[ScannerTrade], *, tolerance_bars: int) -> dict:
    """Assign every event exactly one cause."""
    held: list[tuple[int, int]] = [(t.entry_index, t.exit_index) for t in trades]
    counts = {"CAUGHT": 0, "EARLY": 0, "GATED": 0, "BLIND": 0}
    missed_bps = {"EARLY": 0.0, "GATED": 0.0, "BLIND": 0.0}
    for event in events:
        span = range(event.index, event.index + event.bars_to_complete + 1)
        in_position = any(
            entry <= b <= exit_ for b in span for entry, exit_ in held
        )
        if in_position:
            covered = any(
                entry <= event.index and exit_ >= event.index + event.bars_to_complete
                for entry, exit_ in held
            )
            key = "CAUGHT" if covered else "EARLY"
        else:
            near = any(
                b in armed
                for b in range(event.index - tolerance_bars,
                               event.index + tolerance_bars + 1)
            )
            key = "GATED" if near else "BLIND"
        counts[key] += 1
        if key != "CAUGHT":
            missed_bps[key] += event.move_bps
    return {"counts": counts, "missed_bps": missed_bps, "total": len(events)}


def describe_market(bars: list[tuple], start_i: int) -> dict:
    window = bars[start_i:]
    if len(window) < 2:
        return {}
    opening = window[0][1]
    closing = window[-1][4]
    high = max(b[2] for b in window)
    low = min(b[3] for b in window)
    hourly: list[float] = []
    for i in range(0, len(window) - BARS_PER_HOUR, BARS_PER_HOUR):
        chunk = window[i : i + BARS_PER_HOUR]
        hourly.append((chunk[-1][4] / chunk[0][1] - 1) * 1e4)
    net = (closing / opening - 1) * 1e4
    top5 = sorted(hourly, key=abs, reverse=True)[:5]
    ups = sum(1 for x in hourly if x > 0)
    return {
        "net_bps": net,
        "range_bps": (high - low) / opening * 1e4,
        "hours": len(hourly),
        "top5_share": (sum(top5) / net * 100) if net else float("nan"),
        "persistence": 100 * ups / len(hourly) if hourly else float("nan"),
        "best_hour": max(hourly, key=abs) if hourly else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--event-bps", type=float, default=60.0)
    parser.add_argument("--event-hours", type=int, default=8)
    parser.add_argument("--tolerance-bars", type=int, default=6)
    args = parser.parse_args()

    now = int(dt.datetime.now(UTC).timestamp() * 1000)
    end = now - now % 300_000
    warm = end - 40 * 86_400_000
    tapes = {s.strip(): load(s.strip(), warm, end) for s in args.symbols.split(",")}

    arms = {
        "coil": lambda: CoilArmSource(),
        "ignition": lambda: IgnitionArmSource(),
        "hybrid": lambda: CompositeArmSource(
            sources=[CoilArmSource(), IgnitionArmSource()]
        ),
    }

    for label, days in WINDOWS:
        start_ms = end - days * 86_400_000
        print(f"\n{'=' * 86}\n{label.upper()}   "
              f"({dt.datetime.fromtimestamp(start_ms / 1000, UTC):%d %b %H:%M} -> "
              f"{dt.datetime.fromtimestamp(end / 1000, UTC):%d %b %H:%M} UTC)")

        for symbol, bars in tapes.items():
            start_i = next((i for i, b in enumerate(bars) if b[0] >= start_ms), len(bars))
            if start_i >= len(bars) - 2:
                continue
            market = describe_market(bars, start_i)
            events = find_events(bars, start_i, event_bps=args.event_bps,
                                 event_hours=args.event_hours)
            print(f"\n  {symbol}  net {market['net_bps']:+.0f}bps · range "
                  f"{market['range_bps']:.0f}bps · top-5 hours = "
                  f"{market['top5_share']:.0f}% of net · persistence "
                  f"{market['persistence']:.0f}% · best hour {market['best_hour']:+.0f}bps")
            print(f"    expansion events (>={args.event_bps:.0f}bps within "
                  f"{args.event_hours}h): {len(events)}")
            print(f"      {'arm':<10}{'n':>4}{'PF':>7}{'net bps':>9}  "
                  f"{'CAUGHT':>7}{'EARLY':>7}{'GATED':>7}{'BLIND':>7}   missed bps")
            for name, factory in arms.items():
                session = ScannerSession(symbol=symbol, arm_source=factory())
                trades = session.run(bars, start_ms=start_ms)
                armed = arm_index(bars, start_i, factory)
                report = attribute(events, armed, trades,
                                   tolerance_bars=args.tolerance_bars)
                stats = summarize(trades)
                c = report["counts"]
                pf = "inf" if stats["pf"] == float("inf") else f"{stats['pf']:.2f}"
                missed = sum(report["missed_bps"].values())
                print(
                    f"      {name:<10}{stats['n']:>4}{pf:>7}{stats['net_bps']:>+9.0f}  "
                    f"{c['CAUGHT']:>7}{c['EARLY']:>7}{c['GATED']:>7}{c['BLIND']:>7}"
                    f"{missed:>13.0f}"
                )


if __name__ == "__main__":
    main()
