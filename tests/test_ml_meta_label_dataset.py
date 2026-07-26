"""Meta-label dataset builder: journal parsing + leakage-free feature join."""

import numpy as np
import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix
from vnedge.ml.meta_label_dataset import (
    TradeOutcome,
    build_meta_label_dataset,
    parse_journal_trades,
)

SYMBOL = "ETH/USD:USD"


def _candles(n=500, seed=0):
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = close + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.5, n))
    vol = rng.uniform(100, 1000, n)
    return pd.DataFrame(
        {"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )


def _ms(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def _outcome_record(ts, net, side="long", symbol=SYMBOL, strategy="stealth_trail_bbp_v1"):
    return {
        "kind": "shadow_outcome",
        "intent_key": f"{strategy}|{symbol}|{side}|{_ms(ts)}",
        "virtual_net_usd": net,
        "symbol": symbol,
        "side": side,
        "lane": "papertrial_x",
    }


def test_parse_journal_trades_reads_intent_key_entry_time():
    candles = _candles()
    ts = candles["timestamp"].iloc[300]
    records = [
        {"kind": "lane_eval", "detail": "waiting"},          # ignored
        _outcome_record(ts, 4.2, side="long"),
        {"kind": "shadow_outcome", "intent_key": "bad"},     # malformed -> skipped
    ]
    trades = parse_journal_trades(records)
    assert len(trades) == 1
    t = trades[0]
    assert isinstance(t, TradeOutcome)
    assert t.symbol == SYMBOL and t.side == "long" and t.net_usd == 4.2
    assert t.entry_ts == pd.Timestamp(ts).tz_convert("UTC")


def test_build_dataset_labels_and_features_align():
    candles = _candles()
    entries = candles["timestamp"].iloc[[300, 350, 400]]
    trades = [
        TradeOutcome("stealth_trail_bbp_v1", SYMBOL, "long", pd.Timestamp(entries.iloc[0]), +5.0),
        TradeOutcome("stealth_trail_bbp_v1", SYMBOL, "short", pd.Timestamp(entries.iloc[1]), -3.0),
        TradeOutcome("luxy_ut_bot_forecast_v1", SYMBOL, "long", pd.Timestamp(entries.iloc[2]), +1.0),
    ]
    df, summary = build_meta_label_dataset(trades, {SYMBOL: candles})

    assert summary["samples"] == 3
    assert set(FEATURE_COLUMNS).issubset(df.columns)
    # labels follow net sign
    assert df.sort_values("entry_ts")["meta_label"].tolist() == [1.0, 0.0, 1.0]
    assert abs(summary["win_rate"] - 2 / 3) < 1e-9
    # features are finite and match the causal matrix at the entry bar (no leak)
    fm = build_feature_matrix(candles, None, FeatureParams()).set_index("timestamp")
    for _, r in df.iterrows():
        ref = float(fm.loc[r["entry_ts"], "atr_bps"])
        assert np.isfinite(r["atr_bps"]) and abs(float(r["atr_bps"]) - ref) < 1e-9
    assert summary["by_strategy"]["stealth_trail_bbp_v1"] == 2


def test_build_dataset_drops_warmup_unknown_and_missing():
    candles = _candles()
    warmup_ts = candles["timestamp"].iloc[5]      # warmup -> NaN features
    good_ts = candles["timestamp"].iloc[400]
    trades = [
        TradeOutcome("s", SYMBOL, "long", pd.Timestamp(warmup_ts), 1.0),
        TradeOutcome("s", "DOGE/USD:USD", "long", pd.Timestamp(good_ts), 1.0),  # no candles
        TradeOutcome("s", SYMBOL, "long", pd.Timestamp("2099-01-01", tz="UTC"), 1.0),  # no bar
        TradeOutcome("s", SYMBOL, "long", pd.Timestamp(good_ts), 2.0),  # keeper
    ]
    df, summary = build_meta_label_dataset(trades, {SYMBOL: candles})
    assert summary["samples"] == 1
    assert summary["dropped_no_symbol"] == 1
    assert summary["dropped_no_bar"] == 1
    assert summary["dropped_nan_feature"] == 1


def test_empty_input_is_honest_not_an_error():
    df, summary = build_meta_label_dataset([], {SYMBOL: _candles()})
    assert summary["samples"] == 0 and summary["win_rate"] == 0.0
    assert list(df.columns)[: len(FEATURE_COLUMNS)] == FEATURE_COLUMNS
