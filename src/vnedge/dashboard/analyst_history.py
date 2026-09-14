"""Official Delta OHLC history: an isolated, report-only Analyst data product.

Never writes candle partitions or claims canonical trade completeness. REST
volume is a venue observation, not converted trade volume or exact VWAP.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import math
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import build_opener

from vnedge.dashboard.analyst_public import NoRedirect
from vnedge.dashboard.analyst_store import AnalystStore, digest, utc
from vnedge.dashboard.crypto_analyst import SPEC, TIMEFRAMES, TF_SECONDS, _empty, describe_window

SOURCE = "official_delta_ohlc"
SYMBOLS = ("BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD", "XRPUSD", "ADAUSD",
           "AVAXUSD", "LINKUSD", "LTCUSD", "BCHUSD", "BNBUSD", "DOTUSD")
HISTORY_SPEC = {**SPEC, "version": "crypto_analyst_official_delta_v1",
                "source": SOURCE, "volume": "venue_reported_not_trade_converted",
                "vwap": "unavailable", "universe": SYMBOLS,
                "authority": "analysis_only_no_scanner_ml_or_promotion"}
SPEC_HASH = digest(HISTORY_SPEC)
DEFAULT_PATH = Path("data/analyst_official/evidence.sqlite")
logger = logging.getLogger(__name__)


def validate_scope(exchange: str, symbol: str | None = None, timeframe: str | None = None) -> None:
    if exchange != "delta_india" or (symbol is not None and symbol not in SYMBOLS) or (
        timeframe is not None and timeframe not in TIMEFRAMES
    ):
        raise ValueError("unsupported_official_history_scope")


def normalize(raw: list[dict[str, Any]], symbol: str, timeframe: str, end: int) -> list[dict[str, Any]]:
    validate_scope("delta_india", symbol, timeframe)
    seconds = TF_SECONDS[timeframe]
    if not isinstance(raw, list) or len(raw) > 2000 or end % seconds:
        raise ValueError("official_response_bound")
    rows: dict[int, dict[str, Any]] = {}
    for item in raw:
        stamp = item.get("time")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or int(stamp) != stamp or stamp % seconds:
            raise ValueError("official_timestamp_invalid")
        stamp = int(stamp)
        if stamp + seconds > end or stamp < end - SPEC["window_bars"] * seconds:
            continue
        values: dict[str, float] = {}
        for field in ("open", "high", "low", "close", "volume"):
            value = item.get(field)
            if isinstance(value, bool) or value is None:
                raise ValueError("official_number_invalid")
            value = float(value)
            if not math.isfinite(value) or value < 0 or (field != "volume" and value == 0):
                raise ValueError("official_number_invalid")
            values[field] = value
        if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"]):
            raise ValueError("official_ohlc_invalid")
        row = {**values, "open_time": datetime.fromtimestamp(stamp, UTC).isoformat(),
               "close_time": datetime.fromtimestamp(stamp + seconds, UTC).isoformat(),
               "symbol": symbol, "timeframe": timeframe, "exchange": "delta_india",
               "source": SOURCE, "is_closed": True}
        row["content_sha256"] = digest(row)
        if stamp in rows and rows[stamp] != row:
            raise ValueError("official_conflicting_duplicate")
        rows[stamp] = row
    return [rows[k] for k in sorted(rows)]


def fetch(symbol: str, timeframe: str, end: int) -> list[dict[str, Any]]:
    validate_scope("delta_india", symbol, timeframe)
    query = urlencode({"symbol": symbol, "resolution": timeframe,
                       "start": end - SPEC["window_bars"] * TF_SECONDS[timeframe], "end": end})
    with build_opener(NoRedirect()).open("https://api.india.delta.exchange/v2/history/candles?" + query, timeout=15) as response:
        data = response.read(4_000_001)
    if len(data) > 4_000_000:
        raise ValueError("official_response_too_large")
    body = json.loads(data)
    if body.get("success") is not True or not isinstance(body.get("result"), list):
        raise ValueError("official_request_unsuccessful")
    return body["result"]


def frame(store: AnalystStore, symbol: str, timeframe: str, now: datetime) -> dict[str, Any]:
    validate_scope("delta_india", symbol, timeframe)
    def unavailable(reason: str) -> dict[str, Any]:
        return {**_empty(symbol, timeframe, reason), "source": SOURCE, "can_promote": False}
    try:
        records = store.read("official_series", f"{symbol}/{timeframe}", now=now)
        if not records:
            return unavailable("official_history_not_collected")
        record = records[0]
        body = record["body"]
        if body["request_end"] > now.timestamp():
            return unavailable("official_future_window")
        if body["source"] != SOURCE or body["symbol"] != symbol or body["timeframe"] != timeframe:
            return unavailable("official_scope_mismatch")
        rows = normalize(body["raw"], symbol, timeframe, body["request_end"])
        if not rows:
            return unavailable("official_history_empty")
        suffix = [rows[-1]]
        for row in reversed(rows[:-1]):
            if row["close_time"] != suffix[0]["open_time"]:
                break
            suffix.insert(0, row)
        history = {"required_bars": SPEC["minimum_bars"], "contiguous_bars": len(suffix),
                   "status": "ready" if len(suffix) >= SPEC["minimum_bars"] else "collecting",
                   "last_close": suffix[-1]["close_time"], "source": SOURCE}
        if len(suffix) < SPEC["minimum_bars"]:
            return {**unavailable("official_contiguous_history_insufficient"), "history": history}
        result = describe_window(suffix, symbol, "delta_india", timeframe, now, history,
                                 "official_history_gap" if len(suffix) != len(rows) else None,
                                 source=SOURCE, spec_hash=SPEC_HASH)
        result.update({"evidence_id": record["evidence_id"], "collected_at": record["available_at"],
                       "can_promote": False, "data_contract": HISTORY_SPEC["version"]})
        return result
    except Exception as exc:  # noqa: BLE001 - visibly fail this cell, not the whole dashboard
        logger.warning("official_history_read_failed %s/%s %s", symbol, timeframe, type(exc).__name__)
        return unavailable("official_history_invalid")


class OfficialAnalystService:
    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.store = AnalystStore(path)

    def snapshot(self, exchange: str, timeframe: str) -> dict[str, Any]:
        validate_scope(exchange, timeframe=timeframe)
        now = datetime.now(UTC)
        markets = [frame(self.store, symbol, timeframe, now) for symbol in SYMBOLS]
        current = [r for r in markets if r["state"] == "current"]
        benchmark = next((r for r in current if r["symbol"] == "BTCUSD"), None)
        for row in markets:
            row["metrics"]["relative_btc_12_pct"] = None
            if benchmark and row["state"] == "current" and row["as_of"] == benchmark["as_of"]:
                row["metrics"]["relative_btc_12_pct"] = row["metrics"]["return_12_pct"] - benchmark["metrics"]["return_12_pct"]
                row["benchmark_ref"] = {"symbol": "BTCUSD", "series_hash": benchmark["series_hash"], "as_of": benchmark["as_of"]}
                row["analysis_id"] = digest([row["analysis_id"], row["benchmark_ref"]])
        as_of = max((r["as_of"] for r in current), default=None)
        synchronized = [r for r in current if r["as_of"] == as_of]
        counts = Counter(r["bias"] for r in synchronized)
        markets.sort(key=lambda r: (r["state"] != "current", -abs(r["alignment"] or 0), r["symbol"]))
        return {"schema": HISTORY_SPEC["version"], "source": SOURCE, "spec_hash": SPEC_HASH,
                "generated_at": now.isoformat(), "exchange": exchange, "timeframe": timeframe,
                "can_trade": False, "can_promote": False, "session": "UTC · official history",
                "universe": {"scope": "12-market official history; separate from canonical lake", "discovered": len(SYMBOLS), "displayed": len(markets), "current": len(current), "truncated": False},
                "breadth": {**{k: counts[k] for k in ("bullish", "bearish", "mixed")}, "denominator": len(synchronized), "as_of": as_of},
                "brief": f"{len(current)} of {len(SYMBOLS)} official-history profiles are current. Descriptive alignment only; not canonical scanner or promotion evidence. Exact session VWAP is unavailable.",
                "markets": markets, "methodology": HISTORY_SPEC}


def collect_cell(store: AnalystStore, symbol: str, timeframe: str, now: datetime) -> bool:
    end = int(now.timestamp() - 5) // TF_SECONDS[timeframe] * TF_SECONDS[timeframe]
    old = store.read("official_series", f"{symbol}/{timeframe}", now=now)
    if old and old[0]["body"]["request_end"] == end:
        return False
    raw = fetch(symbol, timeframe, end)
    rows = normalize(raw, symbol, timeframe, end)
    if not rows or utc(rows[-1]["close_time"]).timestamp() != end:
        raise ValueError("official_latest_closed_bar_missing")
    store.append("official_series", f"{symbol}/{timeframe}",
                 {"source": SOURCE, "symbol": symbol, "timeframe": timeframe,
                  "request_end": end, "raw": raw}, datetime.now(UTC))
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_PATH.with_suffix(".lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = AnalystStore(DEFAULT_PATH, writable=True)
        while True:
            issues = []
            for tf in ("15m", "5m", "1h", "4h"):
                for symbol in SYMBOLS:
                    now = datetime.now(UTC)
                    store.append("collector", "delta_india", {"generated_at": now.isoformat(), "issues": issues, "active_cell": f"{symbol}/{tf}"}, now)
                    try:
                        if collect_cell(store, symbol, tf, now):
                            time.sleep(0.4)
                    except Exception as exc:  # noqa: BLE001
                        issue = f"{symbol}/{tf}:{type(exc).__name__}"
                        issues.append(issue)
                        logger.warning("official_collection_failed %s", issue)
                        time.sleep(2)
            now = datetime.now(UTC)
            store.append("collector", "delta_india", {"generated_at": now.isoformat(), "issues": issues, "active_cell": None}, now)
            if args.once:
                return
            time.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
