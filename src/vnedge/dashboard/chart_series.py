"""Chart series for the operator cockpit: canonical candles plus trade markers.

Two things the pulse endpoints do not provide and forensics needs:

* raw OHLCV from the CANONICAL store, so what is charted is the same series
  research and shadow are supposed to read -- not a fourth source invented for
  the UI;
* the lane's own fills as markers on those candles, so "why did it arm there"
  is a glance rather than a log-reading exercise.

Read-only. This module has no order, promotion or settings authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

UTC = timezone.utc

#: Never hand an unbounded series to a browser; a year of 1m bars is 525k rows.
MAX_BARS = 5_000


@dataclass(frozen=True, slots=True)
class ChartMarker:
    """One journal event pinned to a bar time."""

    time: int          # epoch seconds, the unit lightweight-charts expects
    position: str      # aboveBar | belowBar
    shape: str         # arrowUp | arrowDown | circle
    color: str
    text: str


def candles_payload(store: Any, symbol: str, timeframe: str, *,
                    limit: int = 500) -> dict[str, Any]:
    """Canonical OHLCV shaped for lightweight-charts.

    Decimals become floats only at this boundary. They stay Decimal everywhere
    the money math happens; JSON has no decimal type and the chart needs none.
    """
    limit = max(1, min(int(limit), MAX_BARS))
    try:
        rows = store.read(symbol, timeframe)
    except Exception:
        rows = []
    tail = list(rows)[-limit:]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": "canonical_lake",
        "count": len(tail),
        "truncated": len(rows) > len(tail),
        "candles": [
            {
                "time": int(c.open_time.replace(tzinfo=UTC).timestamp())
                if c.open_time.tzinfo is None
                else int(c.open_time.timestamp()),
                "open": float(c.open), "high": float(c.high),
                "low": float(c.low), "close": float(c.close),
                "volume": float(c.volume),
            }
            for c in tail
        ],
    }


def _event_time(payload: dict) -> int | None:
    for key in ("bar_ts", "entry_bar_ts", "ts", "timestamp"):
        raw = payload.get(key)
        if raw is None:
            continue
        if isinstance(raw, (int, float)):
            value = float(raw)
            return int(value / 1000 if value > 1e11 else value)
        try:
            return int(datetime.fromisoformat(str(raw)).timestamp())
        except ValueError:
            continue
    return None


def markers_from_journal(lines: Iterable[str], symbol: str, *,
                         limit: int = 500) -> list[ChartMarker]:
    """Entry and exit markers for one symbol from a decision journal.

    Shadow outcomes carry both legs in one record, so each produces two
    markers: where the lane got in, and where it got out and why. Exit colour
    encodes the outcome, because a red exit under a green entry is the shape a
    reader is looking for.
    """
    out: list[ChartMarker] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (record.get("kind") or record.get("event")) != "shadow_outcome":
            continue
        payload = record.get("payload", record)
        if payload.get("symbol") not in (symbol, symbol.replace("/", "")):
            continue
        side = str(payload.get("side", ""))
        entry_ts = _event_time({"bar_ts": payload.get("entry_bar_ts")})
        exit_ts = _event_time(payload)
        net = payload.get("virtual_net_usd")
        won = isinstance(net, (int, float)) and net > 0
        if entry_ts is not None:
            out.append(ChartMarker(
                time=entry_ts,
                position="belowBar" if side == "long" else "aboveBar",
                shape="arrowUp" if side == "long" else "arrowDown",
                color="#5FA8D8",
                text=f"{side} in",
            ))
        if exit_ts is not None:
            out.append(ChartMarker(
                time=exit_ts,
                position="aboveBar" if side == "long" else "belowBar",
                shape="circle",
                color="#5FBF87" if won else "#E0705E",
                text=f"{payload.get('resolution', 'exit')}"
                     + (f" {net:+.2f}" if isinstance(net, (int, float)) else ""),
            ))
    out.sort(key=lambda m: m.time)
    return out[-limit:]


def markers_payload(journal_dir: Path | None, symbol: str, *,
                    limit: int = 500) -> dict[str, Any]:
    """Markers across every journal in the directory, newest last."""
    if journal_dir is None or not Path(journal_dir).exists():
        return {"symbol": symbol, "count": 0, "markers": [], "journals": 0}
    markers: list[ChartMarker] = []
    files = sorted(Path(journal_dir).glob("*.journal.jsonl"))
    for path in files:
        try:
            with path.open() as handle:
                markers += markers_from_journal(handle, symbol, limit=limit)
        except OSError:
            continue
    markers.sort(key=lambda m: m.time)
    trimmed = markers[-limit:]
    return {
        "symbol": symbol,
        "count": len(trimmed),
        "journals": len(files),
        "markers": [
            {"time": m.time, "position": m.position, "shape": m.shape,
             "color": m.color, "text": m.text}
            for m in trimmed
        ],
    }
