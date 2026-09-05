from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS
from vnedge.ml.hybrid_feature_matrix import (
    HYBRID_FEATURE_COLUMNS,
    HYBRID_MICRO_COLUMNS,
    HybridFeatureParams,
    build_hybrid_feature_matrix,
    build_microstructure_bar_features,
)
from vnedge.ml.meta_label_dataset import TradeOutcome, build_meta_label_dataset
from vnedge.scalping.microstructure import TopOfBook, TradeTick

SYMBOL = "ETH/USD:USD"


def _candles(n: int = 520, *, freq: str = "5min") -> pd.DataFrame:
    rng = np.random.default_rng(42)
    ts = pd.date_range("2026-07-01", periods=n, freq=freq, tz="UTC")
    close = 1800 + np.cumsum(rng.normal(0, 0.8, n))
    open_ = close + rng.normal(0, 0.25, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.5, n))
    volume = rng.uniform(1000, 5000, n)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def _ms(ts: pd.Timestamp | datetime) -> int:
    return int(pd.Timestamp(ts).timestamp() * 1000)


def _book(ts: pd.Timestamp | datetime, bid_size: float, ask_size: float) -> TopOfBook:
    return TopOfBook(
        symbol=SYMBOL,
        bid=1800.0,
        bid_size=bid_size,
        ask=1800.9,
        ask_size=ask_size,
        event_time=pd.Timestamp(ts).to_pydatetime(),
    )


def _trade(
    ts: pd.Timestamp | datetime,
    price: float,
    qty: float,
    side: str,
) -> TradeTick:
    return TradeTick(
        symbol=SYMBOL,
        price=price,
        quantity=qty,
        taker_side=side,  # type: ignore[arg-type]
        event_time=pd.Timestamp(ts).to_pydatetime(),
    )


def _events_for_bar(candles: pd.DataFrame, index: int):
    start = pd.Timestamp(candles["timestamp"].iloc[index])
    return [
        (_ms(start + timedelta(seconds=10)), "book", _book(start + timedelta(seconds=10), 7, 3)),
        (_ms(start + timedelta(seconds=20)), "trade", _trade(start + timedelta(seconds=20), 1800.5, 2, "buy")),
        (_ms(start + timedelta(seconds=40)), "trade", _trade(start + timedelta(seconds=40), 1801.0, 1, "sell")),
        (_ms(start + timedelta(seconds=50)), "book", _book(start + timedelta(seconds=50), 4, 6)),
    ]


def test_microstructure_events_aggregate_into_their_own_bar_only():
    candles = _candles(20)
    events = _events_for_bar(candles, 5)
    micro = build_microstructure_bar_features(
        candles, events, timeframe="5m", missing_policy="zero"
    )

    active = micro.iloc[5]
    before = micro.iloc[4]
    after = micro.iloc[6]

    assert active["micro_book_events"] == 2
    assert active["micro_trade_events"] == 2
    assert active["micro_coverage"] == 1.0
    assert active["micro_taker_buy_ratio"] == 2 / 3
    assert active["micro_signed_notional_usd"] > 0
    assert abs(float(active["micro_imbalance_last"]) - ((4 - 6) / (4 + 6))) < 1e-12
    assert before["micro_coverage"] == 0.0
    assert after["micro_coverage"] == 0.0


def test_hybrid_feature_matrix_keeps_bar_contract_and_adds_micro_columns():
    candles = _candles()
    params = HybridFeatureParams(timeframe="5m", missing_micro_policy="zero")
    df = build_hybrid_feature_matrix(candles, None, _events_for_bar(candles, 300), params)

    assert HYBRID_FEATURE_COLUMNS[: len(FEATURE_COLUMNS)] == FEATURE_COLUMNS
    for col in HYBRID_MICRO_COLUMNS:
        assert col in df.columns
    assert df.loc[300, "micro_trade_events"] == 2
    assert df.loc[300, "micro_trade_intensity_per_min"] == 0.4


def test_hybrid_micro_features_are_causal_against_future_ticks():
    candles = _candles()
    params = HybridFeatureParams(timeframe="5m", missing_micro_policy="zero")
    before = build_hybrid_feature_matrix(candles, None, _events_for_bar(candles, 300), params)
    future_events = [
        *_events_for_bar(candles, 300),
        *_events_for_bar(candles, 360),
    ]
    after = build_hybrid_feature_matrix(candles, None, future_events, params)

    pd.testing.assert_series_equal(
        before.loc[300, HYBRID_MICRO_COLUMNS].astype(float),
        after.loc[300, HYBRID_MICRO_COLUMNS].astype(float),
        check_names=False,
    )


def test_meta_label_dataset_can_opt_into_hybrid_features():
    candles = _candles()
    entry_ts = pd.Timestamp(candles["timestamp"].iloc[400])
    trade = TradeOutcome("stealth_trail_bbp_v1", SYMBOL, "long", entry_ts, 4.2)
    params = HybridFeatureParams(timeframe="5m", missing_micro_policy="zero")

    frame, summary = build_meta_label_dataset(
        [trade],
        {SYMBOL: candles},
        hybrid_params=params,
        micro_events_by_symbol={SYMBOL: _events_for_bar(candles, 400)},
    )

    assert summary["feature_contract"] == "hybrid_bar_microstructure_v1"
    assert summary["feature_columns"] == len(HYBRID_FEATURE_COLUMNS)
    assert set(HYBRID_FEATURE_COLUMNS).issubset(frame.columns)
    assert frame["micro_trade_events"].iloc[0] == 2
    assert frame["meta_label"].iloc[0] == 1.0


def test_meta_label_dataset_bar_only_contract_is_unchanged():
    candles = _candles()
    entry_ts = pd.Timestamp(candles["timestamp"].iloc[400])
    trade = TradeOutcome("stealth_trail_bbp_v1", SYMBOL, "long", entry_ts, 4.2)

    frame, summary = build_meta_label_dataset([trade], {SYMBOL: candles})

    assert summary["feature_contract"] == "bar_v1"
    assert list(frame.columns)[: len(FEATURE_COLUMNS)] == FEATURE_COLUMNS
    for col in HYBRID_MICRO_COLUMNS:
        assert col not in frame.columns
