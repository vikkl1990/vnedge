"""Independent, read-only crypto technical analyst. Never an execution scanner.

All scores describe observable alignment, NOT probabilities or expected returns.
No strategy/runtime/ML registry imports, external requests, or lake writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.data.candles import TF_SECONDS, _decision_hash_row

SPEC = {
    "version": "crypto_analyst_v1",
    "window_bars": 512,
    "minimum_bars": 60,
    "ema": "20/50 adjust=False first-valid seed on bounded analysis window",
    "momentum": "12-bar return / mean true range14; clipped to [-1,1]",
    "structure": "close location in prior20 high/low range; clipped to [-1,1]",
    "participation": "relative volume20 (prior only); signed by candle body",
    "weights": {"trend": 35, "momentum": 25, "structure": 25, "participation": 15},
    "bias_threshold": 20,
    "compression_ratio": 0.75,
    "expansion_ratio": 1.5,
    "range_bars": 20,
    "rvol_baseline_bars": 20,
    "rvol_expansion": 2,
    "true_range": "mean14 / preceding28 mean for volatility state; not Wilder ATR",
    "vwap_recovery": "current low <= exact session mean < bullish closed close",
    "session_reset": "00:00 UTC",
    "freshness": "last close age <= 1.5*timeframe",
    "authority": "analysis_only",
    "universe_cap": 24,
}
SPEC_HASH = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()
EXCHANGES = ("delta_india", "binanceusdm", "bybit")
TIMEFRAMES = ("5m", "15m", "1h", "4h")
logger = logging.getLogger(__name__)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _true(value: Any) -> bool:
    return isinstance(value, (bool, np.bool_)) and bool(value)


def _empty(symbol: str, timeframe: str, reason: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "state": "unavailable",
        "bias": "unknown",
        "alignment": None,
        "coverage_pct": 0,
        "issues": [reason],
        "supports": [],
        "conflicts": [],
        "metrics": {},
        "components": [],
        "setups": [],
        "sparkline": [],
        "as_of": None,
        "analysis_id": None,
        "can_trade": False,
    }


def analyse_rows(
    rows: list[dict[str, Any]], symbol: str, exchange: str, timeframe: str, now: datetime
) -> dict[str, Any]:
    """As-of projection: future/forming bars never contribute to any feature."""
    if timeframe not in TIMEFRAMES or exchange not in EXCHANGES:
        raise ValueError("unsupported analyst scope")
    if now.tzinfo is None:
        raise ValueError("timezone-aware now required")
    seconds = TF_SECONDS[timeframe]
    by_open: dict[int, tuple[dict[str, Any], str | None]] = {}
    try:
        for row in rows:
            opened, closed = pd.Timestamp(row["open_time"]), pd.Timestamp(row["close_time"])
            if opened.tzinfo is None or closed.tzinfo is None:
                raise ValueError("naive timestamp")
            if closed > now:
                continue
            closed_flag = row.get("is_closed")
            if not isinstance(closed_flag, (bool, np.bool_)):
                return _empty(symbol, timeframe, "closed_bar_proof_missing")
            if not closed_flag:
                continue
            epoch = int(opened.timestamp())
            problem = None
            if (
                opened.microsecond
                or opened.nanosecond
                or epoch % seconds
                or (closed - opened).total_seconds() != seconds
            ):
                problem = "bar_clock_invalid"
            elif row.get("source") != "canonical_tick_lake":
                problem = "noncanonical_source"
            elif row.get("data_quality") != "ok" or not _true(row.get("coverage_ok")):
                problem = "bar_coverage_unverified"
            elif any(
                row.get(k) is not None and row[k] != v
                for k, v in (("exchange", exchange), ("symbol", symbol), ("timeframe", timeframe))
            ):
                problem = "series_identity_mismatch"
            else:
                expected = bar_content_sha256(
                    _decision_hash_row(row),
                    open_time=opened.to_pydatetime(),
                    close_time=closed.to_pydatetime(),
                    source="canonical_tick_lake",
                )
                if expected != row.get("content_sha256"):
                    problem = "bar_hash_invalid"
            values = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
            o, h, low, c, v = values
            if (
                not all(np.isfinite(values))
                or min(o, h, low, c) <= 0
                or v < 0
                or not low <= min(o, c) <= max(o, c) <= h
            ):
                problem = "ohlcv_invalid"
            if epoch in by_open and _digest(by_open[epoch][0]) != _digest(row):
                return _empty(symbol, timeframe, "conflicting_duplicate")
            by_open[epoch] = (row, problem)
    except (KeyError, ValueError, TypeError, OverflowError):
        return _empty(symbol, timeframe, "malformed_bar")
    ordered = sorted(by_open.items())[-SPEC["window_bars"] :]
    if not ordered:
        return _empty(symbol, timeframe, "no_closed_bars")
    # A bad latest close must not be replaced by a quietly carried old close.
    if ordered[-1][1][1]:
        return _empty(symbol, timeframe, ordered[-1][1][1])
    suffix: list[dict[str, Any]] = []
    next_epoch = ordered[-1][0] + seconds
    cut_reason = None
    for epoch, (row, problem) in reversed(ordered):
        if problem or epoch != next_epoch - seconds:
            cut_reason = problem or "history_gap"
            break
        suffix.append(row)
        next_epoch = epoch
    suffix.reverse()
    if len(suffix) < SPEC["minimum_bars"]:
        result = _empty(symbol, timeframe, "insufficient_contiguous_history")
        result.update(
            bars=len(suffix), issues=[x for x in (cut_reason, "need_60_contiguous_bars") if x]
        )
        return result
    df = pd.DataFrame(suffix)
    o, h, low, close, vol = (
        pd.to_numeric(df[k]).astype(float) for k in ("open", "high", "low", "close", "volume")
    )
    price = float(close.iloc[-1])
    ema20 = float(close.ewm(span=20, adjust=False, min_periods=20).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False, min_periods=50).mean().iloc[-1])
    tr = pd.concat([h - low, (h - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(
        axis=1
    )
    atr = float(tr.tail(14).mean())
    previous_atr = float(tr.iloc[-42:-14].mean())
    vol_ratio = atr / previous_atr if previous_atr > 0 else None
    range_high, range_low = float(h.iloc[-21:-1].max()), float(low.iloc[-21:-1].min())
    prior_volume = float(vol.iloc[-21:-1].mean())
    rvol = float(vol.iloc[-1]) / prior_volume if prior_volume > 0 else None
    roc = (price / float(close.iloc[-13]) - 1) * 100

    def clip(x: float) -> float:
        return float(np.clip(x, -1, 1))

    trend = 1.0 if price > ema20 > ema50 else -1.0 if price < ema20 < ema50 else 0.0
    momentum = clip((price - float(close.iloc[-13])) / (atr * 3)) if atr > 0 else 0.0
    structure = (
        clip(2 * (price - range_low) / (range_high - range_low) - 1)
        if range_high > range_low
        else 0.0
    )
    participation = (
        clip(rvol - 1) * float(np.sign(price - float(o.iloc[-1]))) if rvol is not None else None
    )
    raw = {
        "trend": trend,
        "momentum": momentum,
        "structure": structure,
        "participation": participation,
    }
    components = [
        {
            "name": k,
            "weight": w,
            "value": None if raw[k] is None else round(raw[k], 4),
            "contribution": None if raw[k] is None else round(raw[k] * w, 2),
        }
        for k, w in SPEC["weights"].items()
    ]
    score = sum(x["contribution"] or 0 for x in components)
    bias = "bullish" if score >= 20 else "bearish" if score <= -20 else "mixed"
    vwap = None
    latest_close = pd.Timestamp(suffix[-1]["close_time"])
    # Session belongs to the last anchor, including the midnight closing bar.
    session_open = pd.Timestamp(suffix[-1]["open_time"]).normalize()
    session = [r for r in suffix if pd.Timestamp(r["open_time"]) >= session_open]
    if session and pd.Timestamp(session[0]["open_time"]) == session_open:
        try:
            q = [float(r["quote_volume"]) for r in session]
            b = [float(r["volume"]) for r in session]
            if all(np.isfinite(x) and x > 0 for x in q + b):
                vwap = sum(q) / sum(b)
        except (KeyError, ValueError, TypeError):
            pass
    supports, conflicts = [], []
    descriptions = {
        "trend": "Price and EMA20/50 stack",
        "momentum": "12-bar price momentum",
        "structure": "Location versus prior 20-bar range",
        "participation": "Candle-direction relative volume (not order flow)",
    }
    direction = 1 if bias == "bullish" else -1 if bias == "bearish" else 0
    for c in components:
        value = c["contribution"]
        text = (
            f"{descriptions[c['name']]}: {value:+.1f} points"
            if value is not None
            else f"{descriptions[c['name']]}: unavailable"
        )
        (supports if value is not None and value * direction > 0 else conflicts).append(text)
    setups = []
    if price > range_high:
        setups.append("upside_breakout")
    if price < range_low:
        setups.append("downside_breakout")
    if vol_ratio is not None and vol_ratio < 0.75:
        setups.append("compression")
    if rvol is not None and rvol >= 2:
        setups.append("volume_expansion")
    if vwap is not None and float(low.iloc[-1]) <= vwap < price and price > float(o.iloc[-1]):
        setups.append("vwap_recovery")
    if trend and "upside_breakout" not in setups and "downside_breakout" not in setups:
        setups.append("trend_watch")
    age = (now - latest_close.to_pydatetime()).total_seconds()
    issues = [x for x in (cut_reason, "session_vwap_unavailable" if vwap is None else None) if x]
    if age > seconds * 1.5:
        issues.append("stale_series")
    source_refs = [
        {"open_time": str(r["open_time"]), "content_sha256": r["content_sha256"]} for r in suffix
    ]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "exchange": exchange,
        "state": "stale" if age > seconds * 1.5 else "current",
        "bias": bias,
        "alignment": round(score, 1),
        "coverage_pct": sum(c["weight"] for c in components if c["value"] is not None),
        "bars": len(suffix),
        "as_of": latest_close.isoformat(),
        "age_s": age,
        "analysis_id": _digest([SPEC_HASH, exchange, symbol, timeframe, source_refs]),
        "series_hash": _digest(source_refs),
        "anchor_hash": suffix[-1]["content_sha256"],
        "source": "canonical_tick_lake",
        "supports": supports,
        "conflicts": conflicts,
        "issues": issues,
        "components": components,
        "setups": setups,
        "metrics": {
            "price": price,
            "return_12_pct": roc,
            "ema20": ema20,
            "ema50": ema50,
            "atr_pct": atr / price * 100,
            "volume_ratio": rvol,
            "volatility_ratio": vol_ratio,
            "range_high": range_high,
            "range_low": range_low,
            "session_vwap": vwap,
            "vwap_distance_bps": (price / vwap - 1) * 10000 if vwap else None,
        },
        "scenario": {
            "upside": f"Watch a closed hold above {range_high:.8g}; a move below {range_low:.8g} contradicts this range-break thesis.",
            "downside": f"Watch a closed hold below {range_low:.8g}; a move above {range_high:.8g} contradicts this range-break thesis.",
            "neutral": "Inside the reference range: wait for resolution; a range boundary is not an entry order.",
        },
        "sparkline": [float(x) for x in close.tail(40)],
        "can_trade": False,
        "execution": {
            "spread_bps": None,
            "cost_profile_id": None,
            "net_edge_bps": None,
            "status": "not_assessed",
        },
        "unavailable_inputs": [
            "BBO/spread",
            "settled funding",
            "open interest",
            "aggressor flow",
            "validated ML probability",
        ],
    }


def read_window(
    root: Path, exchange: str, symbol: str, timeframe: str, now: datetime
) -> list[dict[str, Any]]:
    """At most four recent partitions, with timeframe-specific row bounds."""
    import pyarrow.parquet as pq

    directory = root / f"exchange={exchange}" / symbol / timeframe
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("unsafe_series_path")
    pattern = (
        r"\d{4}-\d{2}\.parquet" if timeframe in {"1h", "4h"} else r"\d{4}-\d{2}-\d{2}\.parquet"
    )
    ceiling = now.strftime("%Y-%m.parquet" if timeframe in {"1h", "4h"} else "%Y-%m-%d.parquet")
    files = sorted(
        p
        for p in directory.glob("*.parquet")
        if re.fullmatch(pattern, p.name) and p.name <= ceiling
    )[-4:]
    frames = []
    for path in files:
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("partition_read_bound")
        with path.open("rb") as handle:
            parquet = pq.ParquetFile(handle)
            # Twice a full partition permits ordinary overlap, not unbounded
            # arbitrary history disguised as a single daily/monthly file.
            partition_seconds = (31 if timeframe in {"1h", "4h"} else 1) * 86400
            if parquet.metadata.num_rows > 2 * partition_seconds // TF_SECONDS[timeframe] + 10:
                raise ValueError("partition_row_bound")
            frames.append(parquet.read().to_pandas())
    return pd.concat(frames, ignore_index=True).to_dict("records") if frames else []


class CryptoAnalystService:
    """Small on-demand, single-flight cache; no background trading process."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    def snapshot(self, exchange: str, timeframe: str) -> dict[str, Any]:
        if exchange not in EXCHANGES or timeframe not in TIMEFRAMES:
            raise ValueError("unsupported analyst scope")
        key = (exchange, timeframe)
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < 30:
                return cached[1]
            now = datetime.now(UTC)
            directory = self.root / f"exchange={exchange}"
            if (
                not directory.resolve().is_relative_to(self.root.resolve())
                or directory.is_symlink()
            ):
                raise ValueError("unsafe_exchange_path")
            discovered = sorted(
                p.name
                for p in directory.glob("*")
                if p.is_dir() and not p.is_symlink() and re.fullmatch(r"[A-Z0-9]{3,30}", p.name)
            )
            default = ["BTCUSD", "ETHUSD"] if exchange == "delta_india" else ["BTCUSDT", "ETHUSDT"]
            symbols = list(dict.fromkeys(default + discovered))[: SPEC["universe_cap"]]
            results = []
            for symbol in symbols:
                try:
                    rows = read_window(self.root, exchange, symbol, timeframe, now)
                    result = analyse_rows(rows, symbol, exchange, timeframe, now)
                except Exception as exc:  # noqa: BLE001 - fail visible at per-market read boundary
                    logger.warning(
                        "analyst_series_read_failed exchange=%s symbol=%s error=%s",
                        exchange,
                        symbol,
                        type(exc).__name__,
                    )
                    result = _empty(symbol, timeframe, "series_read_failed")
                results.append(result)
            current = [r for r in results if r["state"] == "current"]
            benchmark = next((r for r in current if r["symbol"] == default[0]), None)
            for result in results:
                result["metrics"]["relative_btc_12_pct"] = (
                    result["metrics"]["return_12_pct"] - benchmark["metrics"]["return_12_pct"]
                    if benchmark
                    and result["state"] == "current"
                    and result["as_of"] == benchmark["as_of"]
                    else None
                )
                if result["metrics"]["relative_btc_12_pct"] is not None:
                    result["benchmark_ref"] = {
                        "symbol": benchmark["symbol"],
                        "series_hash": benchmark["series_hash"],
                        "as_of": benchmark["as_of"],
                    }
                    result["analysis_id"] = _digest(
                        [result["analysis_id"], result["benchmark_ref"]]
                    )
            breadth_as_of = max((r["as_of"] for r in current), default=None)
            synchronized = [r for r in current if r["as_of"] == breadth_as_of]
            counts = Counter(r["bias"] for r in synchronized)
            results.sort(
                key=lambda r: (r["state"] != "current", -abs(r["alignment"] or 0), r["symbol"])
            )
            payload = {
                "schema": SPEC["version"],
                "spec_hash": SPEC_HASH,
                "generated_at": now.isoformat(),
                "exchange": exchange,
                "timeframe": timeframe,
                "can_trade": False,
                "can_promote": False,
                "session": "Asia UTC 00–08"
                if now.hour < 8
                else "Europe UTC 08–16"
                if now.hour < 16
                else "Americas UTC 16–24",
                "universe": {
                    "scope": "local canonical lake coverage, not all venue products",
                    "discovered": len(discovered),
                    "displayed": len(results),
                    "current": len(current),
                    "truncated": len(set(default + discovered)) > len(symbols),
                },
                "breadth": {
                    "bullish": counts["bullish"],
                    "bearish": counts["bearish"],
                    "mixed": counts["mixed"],
                    "denominator": len(synchronized),
                    "as_of": breadth_as_of,
                },
                "brief": (
                    f"{len(current)} of {len(results)} covered markets are current. "
                    f"At the newest common close: {counts['bullish']} bullish, {counts['bearish']} bearish, {counts['mixed']} mixed technical profiles. "
                    "Rankings describe alignment, not expected profit."
                ),
                "markets": results,
                "methodology": SPEC,
            }
            self._cache[key] = (time.monotonic(), payload)
            return payload
