"""Hybrid bar + microstructure feature matrix for ML research.

The existing ``feature_matrix`` contract is bar-only and order-stable because
saved models depend on it. This module keeps that contract intact and exposes a
new opt-in feature set for models that should learn from both closed candles and
recorded tick/L2 flow.

Causality rule: row i uses only the closed candle at row i and microstructure
events whose timestamps fall inside that candle's own interval. Future book or
trade events never modify earlier feature rows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import pstdev
from typing import Iterable, Literal

import pandas as pd

from vnedge.data.schemas import TIMEFRAME_MS
from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix
from vnedge.scalping.microstructure import TopOfBook, TradeTick

MicroEvent = tuple[int, str, object]
MissingMicroPolicy = Literal["nan", "zero"]

HYBRID_MICRO_COLUMNS = [
    "micro_book_events",
    "micro_trade_events",
    "micro_coverage",
    "micro_spread_bps_mean",
    "micro_spread_bps_p95",
    "micro_imbalance_mean",
    "micro_imbalance_last",
    "micro_microprice_bps_last",
    "micro_top_depth_usd_mean",
    "micro_trade_notional_usd",
    "micro_signed_notional_usd",
    "micro_taker_buy_ratio",
    "micro_trade_intensity_per_min",
    "micro_trade_realized_vol_bps",
]

HYBRID_FEATURE_COLUMNS = [*FEATURE_COLUMNS, *HYBRID_MICRO_COLUMNS]


@dataclass(frozen=True)
class HybridFeatureParams:
    """Configuration for the opt-in hybrid feature matrix."""

    bar: FeatureParams = field(default_factory=FeatureParams)
    timeframe: str | None = None
    default_bar_ms: int | None = None
    missing_micro_policy: MissingMicroPolicy = "nan"

    def bar_ms(self, candles: pd.DataFrame) -> int:
        if self.timeframe:
            try:
                return TIMEFRAME_MS[self.timeframe]
            except KeyError as exc:
                raise ValueError(f"unsupported timeframe: {self.timeframe}") from exc
        if self.default_bar_ms is not None:
            if self.default_bar_ms <= 0:
                raise ValueError("default_bar_ms must be positive")
            return int(self.default_bar_ms)
        ts = pd.to_datetime(candles["timestamp"], utc=True)
        if len(ts) >= 2:
            deltas = ts.diff().dropna().dt.total_seconds()
            deltas = deltas[deltas > 0]
            if len(deltas):
                return int(round(float(deltas.median()) * 1000.0))
        raise ValueError("cannot infer bar interval; pass timeframe or default_bar_ms")


def build_hybrid_feature_matrix(
    candles: pd.DataFrame,
    funding: pd.DataFrame | None,
    micro_events: Iterable[MicroEvent] | None = None,
    params: HybridFeatureParams = HybridFeatureParams(),
) -> pd.DataFrame:
    """Return closed-bar features plus optional tick/L2 aggregates.

    ``micro_events`` accepts the same ``(ts_ms, kind, object)`` tuples returned by
    ``vnedge.scalping.replay_backtester.load_tick_events``. The objects may be
    ``TopOfBook`` or ``TradeTick``. Missing microstructure data remains visible:
    counts are zero, ``micro_coverage`` is 0, and continuous fields are NaN
    unless ``missing_micro_policy='zero'`` is requested for research experiments
    that deliberately want a dense matrix.
    """

    base = build_feature_matrix(candles, funding, params.bar)
    micro = build_microstructure_bar_features(
        candles,
        micro_events or (),
        timeframe=params.timeframe,
        default_bar_ms=params.default_bar_ms,
        missing_policy=params.missing_micro_policy,
    )
    out = base.merge(micro, on="timestamp", how="left", validate="one_to_one")
    for col in HYBRID_MICRO_COLUMNS:
        if col not in out.columns:
            out[col] = math.nan
    return out


def build_microstructure_bar_features(
    candles: pd.DataFrame,
    micro_events: Iterable[MicroEvent],
    *,
    timeframe: str | None = None,
    default_bar_ms: int | None = None,
    missing_policy: MissingMicroPolicy = "nan",
) -> pd.DataFrame:
    """Aggregate tick/L2 events into causal per-candle rows."""

    params = HybridFeatureParams(timeframe=timeframe, default_bar_ms=default_bar_ms)
    bar_ms = params.bar_ms(candles)
    ts = pd.to_datetime(candles["timestamp"], utc=True).reset_index(drop=True)
    starts_ms = [int(pd.Timestamp(value).value // 1_000_000) for value in ts]
    ends_ms = [start + bar_ms for start in starts_ms]
    events = sorted(_valid_events(micro_events), key=lambda item: (item[0], _kind_rank(item[1])))

    rows: list[dict[str, float | int | pd.Timestamp]] = []
    cursor = 0
    n_events = len(events)
    for idx, start_ms in enumerate(starts_ms):
        end_ms = ends_ms[idx]
        while cursor < n_events and events[cursor][0] < start_ms:
            cursor += 1
        local_cursor = cursor
        books: list[TopOfBook] = []
        trades: list[TradeTick] = []
        while local_cursor < n_events and events[local_cursor][0] < end_ms:
            _, kind, obj = events[local_cursor]
            if kind == "book" and isinstance(obj, TopOfBook):
                books.append(obj)
            elif kind == "trade" and isinstance(obj, TradeTick):
                trades.append(obj)
            local_cursor += 1
        rows.append(_aggregate_bar(ts.iloc[idx], books, trades, bar_ms, missing_policy))

    return pd.DataFrame(rows, columns=["timestamp", *HYBRID_MICRO_COLUMNS])


def _valid_events(events: Iterable[MicroEvent]) -> Iterable[MicroEvent]:
    for raw_ts, kind, obj in events:
        if kind not in {"book", "trade"}:
            continue
        try:
            ts_ms = int(raw_ts)
        except (TypeError, ValueError):
            continue
        if kind == "book" and not isinstance(obj, TopOfBook):
            continue
        if kind == "trade" and not isinstance(obj, TradeTick):
            continue
        yield ts_ms, kind, obj


def _kind_rank(kind: str) -> int:
    return 0 if kind == "book" else 1


def _aggregate_bar(
    timestamp: pd.Timestamp,
    books: list[TopOfBook],
    trades: list[TradeTick],
    bar_ms: int,
    missing_policy: MissingMicroPolicy,
) -> dict[str, float | int | pd.Timestamp]:
    book_events = len(books)
    trade_events = len(trades)
    row: dict[str, float | int | pd.Timestamp] = {
        "timestamp": timestamp,
        "micro_book_events": book_events,
        "micro_trade_events": trade_events,
        "micro_coverage": 1.0 if book_events or trade_events else 0.0,
    }

    if books:
        spreads = [b.spread_bps for b in books]
        imbalances = [b.book_imbalance for b in books]
        depths = [b.top_depth_usd for b in books]
        last = books[-1]
        row.update(
            {
                "micro_spread_bps_mean": float(sum(spreads) / len(spreads)),
                "micro_spread_bps_p95": _percentile(spreads, 0.95),
                "micro_imbalance_mean": float(sum(imbalances) / len(imbalances)),
                "micro_imbalance_last": float(last.book_imbalance),
                "micro_microprice_bps_last": float(
                    (last.microprice - last.mid_price) / last.mid_price * 10_000.0
                ),
                "micro_top_depth_usd_mean": float(sum(depths) / len(depths)),
            }
        )
    else:
        row.update(
            {
                "micro_spread_bps_mean": math.nan,
                "micro_spread_bps_p95": math.nan,
                "micro_imbalance_mean": math.nan,
                "micro_imbalance_last": math.nan,
                "micro_microprice_bps_last": math.nan,
                "micro_top_depth_usd_mean": math.nan,
            }
        )

    if trades:
        total_qty = sum(t.quantity for t in trades)
        buy_qty = sum(t.quantity for t in trades if t.taker_side == "buy")
        notional = sum(t.price * t.quantity for t in trades)
        signed = sum(t.signed_notional_usd for t in trades)
        prices = [t.price for t in trades]
        returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
            if prices[i - 1] > 0 and prices[i] > 0
        ]
        minutes = max(bar_ms / 60_000.0, 1e-9)
        row.update(
            {
                "micro_trade_notional_usd": float(notional),
                "micro_signed_notional_usd": float(signed),
                "micro_taker_buy_ratio": float(buy_qty / total_qty) if total_qty > 0 else 0.0,
                "micro_trade_intensity_per_min": float(len(trades) / minutes),
                "micro_trade_realized_vol_bps": (
                    float(pstdev(returns) * 10_000.0) if len(returns) >= 2 else 0.0
                ),
            }
        )
    else:
        row.update(
            {
                "micro_trade_notional_usd": 0.0,
                "micro_signed_notional_usd": 0.0,
                "micro_taker_buy_ratio": math.nan,
                "micro_trade_intensity_per_min": 0.0,
                "micro_trade_realized_vol_bps": math.nan,
            }
        )

    if missing_policy == "zero":
        for key, value in tuple(row.items()):
            if key == "timestamp":
                continue
            if isinstance(value, float) and math.isnan(value):
                row[key] = 0.0
    return row


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight
