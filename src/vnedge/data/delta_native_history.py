"""Native Delta Exchange India historical data (endpoints CCXT doesn't cover).

CCXT's delta adapter exposes NO funding-rate history, which forced Delta
funding-MR lanes to accumulate the percentile window live over ~10 days.
Delta's own candle history API, however, serves SETTLED FUNDING HISTORY via a
symbol prefix (verified live 2026-07-07):

    GET https://api.india.delta.exchange/v2/history/candles
        ?resolution=1h&symbol=FUNDING:BTCUSD&start=<epoch_s>&end=<epoch_s>

returns hourly candles whose ``close`` is the funding rate in PERCENT
(e.g. ``-0.028`` == -0.028%), newest-first, with ``time`` in epoch SECONDS.
An unknown symbol yields ``{"success": true, "result": []}``; a malformed
request yields ``{"success": false, "error": {...}}``.

This module normalises that feed to the canonical funding frame used
everywhere else ([timestamp tz-aware UTC, funding_rate as a FRACTION] —
matching ``normalize_funding`` output and the ``delta_ws`` /100 convention).
Public data only; no credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable

import pandas as pd

from vnedge.data.schemas import normalize_funding
from vnedge.exchange.delta_ws import delta_native_symbol

logger = logging.getLogger(__name__)

DELTA_INDIA_API_URL = "https://api.india.delta.exchange"

# Exchange ids that mean "Delta India via native APIs" across the codebase
# (mirrors the feed factory's routing set).
DELTA_NATIVE_EXCHANGE_IDS = frozenset({"delta_india", "delta", "deltaindia"})

# Delta caps candle responses around 2000 rows. Page requests in windows
# safely below the cap so a single window can never silently truncate.
_CANDLES_PER_PAGE = 1500
_REQUEST_TIMEOUT_SECONDS = 15.0

_RESOLUTION_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1_800,
    "1h": 3_600,
    "2h": 7_200,
    "4h": 14_400,
    "1d": 86_400,
}


def _http_get_json(url: str) -> dict:
    """Blocking GET returning parsed JSON; run via ``asyncio.to_thread``."""
    with urllib.request.urlopen(url, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def fetch_delta_funding_history(
    symbol: str,
    days: int = 30,
    *,
    resolution: str = "1h",
    base_url: str = DELTA_INDIA_API_URL,
    now_s: int | None = None,
    http_get_json: Callable[[str], dict] | None = None,
) -> pd.DataFrame:
    """Settled funding history from Delta's native ``FUNDING:`` candle feed.

    Returns the canonical funding frame ([timestamp tz-aware UTC,
    funding_rate as a FRACTION]; percent is divided by 100 here), sorted
    ascending and deduped — an empty (properly typed) frame when the venue
    has no data for the range. Raises on HTTP errors or a ``success: false``
    payload so callers choose their own fallback posture.

    ``http_get_json`` is injectable for tests; the default uses urllib in a
    worker thread (no extra HTTP dependency).
    """
    step_s = _RESOLUTION_SECONDS.get(resolution)
    if step_s is None:
        raise ValueError(f"unsupported delta candle resolution: {resolution!r}")
    get = http_get_json or _http_get_json
    native = delta_native_symbol(symbol)
    end_s = int(time.time()) if now_s is None else int(now_s)
    start_s = end_s - int(days) * 86_400
    window_s = _CANDLES_PER_PAGE * step_s

    rows: list[tuple[int, float]] = []
    cursor = start_s
    while cursor < end_s:
        window_end = min(cursor + window_s, end_s)
        query = urllib.parse.urlencode(
            {
                "resolution": resolution,
                "symbol": f"FUNDING:{native}",
                "start": cursor,
                "end": window_end,
            }
        )
        payload = await asyncio.to_thread(get, f"{base_url}/v2/history/candles?{query}")
        if not isinstance(payload, dict) or not payload.get("success", False):
            raise ValueError(f"delta candle API error for FUNDING:{native}: {payload!r}")
        for item in payload.get("result") or []:
            try:
                rows.append((int(item["time"]), float(item["close"])))
            except (KeyError, TypeError, ValueError):
                continue  # tolerate the odd malformed row; the rest still seed
        cursor = window_end

    if not rows:
        logger.info(
            "delta native funding history: no rows for FUNDING:%s (%dd)", native, days
        )
        return normalize_funding([])

    df = pd.DataFrame(rows, columns=["ts_s", "close_pct"])
    # epoch seconds -> ms before to_datetime so the dtype matches
    # normalize_funding output exactly (datetime64[ms, UTC])
    df["timestamp"] = pd.to_datetime(df["ts_s"] * 1_000, unit="ms", utc=True)
    # Delta reports funding as a PERCENT; normalise to the fraction convention
    # used everywhere else (same /100 as the native websocket client).
    df["funding_rate"] = df["close_pct"].astype("float64") / 100.0
    out = (
        df[["timestamp", "funding_rate"]]
        .drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    logger.info(
        "delta native funding history: %d settled prints for FUNDING:%s (%s -> %s)",
        len(out), native, out["timestamp"].iloc[0], out["timestamp"].iloc[-1],
    )
    return out
