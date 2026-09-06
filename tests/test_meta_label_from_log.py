"""Log-backed meta-label dataset: exact-vector join, causal match, counters."""

from __future__ import annotations

import numpy as np
import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS
from vnedge.ml.meta_label_dataset import (
    TradeOutcome,
    build_meta_label_dataset_from_log,
)


def _log(rows: list[dict]) -> pd.DataFrame:
    """Build a feature-log frame with all contract columns present."""
    base = {col: 0.0 for col in FEATURE_COLUMNS}
    return pd.DataFrame(
        [{**base, **r} for r in rows],
        columns=(
            ["ts", "bar_ts", "strategy_id", "symbol", "decision", "intent_key", "backfill"]
            + FEATURE_COLUMNS
        ),
    )


def test_join_uses_logged_vector_verbatim():
    log = _log([
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "decision": "fired",
         "bar_ts": "2026-01-01T12:00:00+00:00", "rsi14": 71.5, "atr_bps": 42.0},
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "decision": "pass",
         "bar_ts": "2026-01-01T12:30:00+00:00", "rsi14": 30.0},
    ])
    trade = TradeOutcome(
        strategy="s1", symbol="BTC/USDT:USDT", side="long",
        entry_ts=pd.Timestamp("2026-01-01T12:05:00+00:00"), net_usd=17.0, lane="L1",
    )
    frame, summary = build_meta_label_dataset_from_log([trade], log)
    assert summary["matched"] == 1 and summary["samples"] == 1
    assert frame["meta_label"].iloc[0] == 1.0
    # the EXACT logged numbers, not a re-derivation
    assert frame["rsi14"].iloc[0] == 71.5
    assert frame["atr_bps"].iloc[0] == 42.0


def test_only_fired_rows_and_causal_at_or_before_entry():
    log = _log([
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "decision": "pass",
         "bar_ts": "2026-01-01T12:00:00+00:00", "rsi14": 10.0},   # pass: ignored
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "decision": "fired",
         "bar_ts": "2026-01-01T13:00:00+00:00", "rsi14": 55.0},   # after entry: ignored
    ])
    trade = TradeOutcome(
        strategy="s1", symbol="BTC/USDT:USDT", side="long",
        entry_ts=pd.Timestamp("2026-01-01T12:30:00+00:00"), net_usd=-5.0,
    )
    frame, summary = build_meta_label_dataset_from_log([trade], log)
    # no fired row at-or-before entry -> unmatched, counted, never guessed
    assert summary["matched"] == 0 and summary["dropped_no_log_row"] == 1
    assert len(frame) == 0


def test_tolerance_bounds_the_match():
    log = _log([
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "decision": "fired",
         "bar_ts": "2026-01-01T00:00:00+00:00", "rsi14": 60.0},
    ])
    trade = TradeOutcome(
        strategy="s1", symbol="BTC/USDT:USDT", side="long",
        entry_ts=pd.Timestamp("2026-01-01T06:00:00+00:00"), net_usd=1.0,
    )
    # default tolerance 900s: a 6h-old arm is too stale
    _, summ_default = build_meta_label_dataset_from_log([trade], log)
    assert summ_default["matched"] == 0
    # widen tolerance: now it matches
    _, summ_wide = build_meta_label_dataset_from_log(
        [trade], log, tolerance_seconds=6 * 3600
    )
    assert summ_wide["matched"] == 1


def test_missing_contract_column_raises():
    partial = pd.DataFrame(
        [{"strategy_id": "s1", "symbol": "X", "decision": "fired",
          "bar_ts": "2026-01-01T00:00:00+00:00", "rsi14": 1.0}]
    )
    trade = TradeOutcome("s1", "X", "long", pd.Timestamp("2026-01-01T00:00:00+00:00"), 1.0)
    try:
        build_meta_label_dataset_from_log([trade], partial)
        raise AssertionError("missing contract columns must raise")
    except ValueError:
        pass


def test_empty_log_returns_empty_not_error():
    frame, summary = build_meta_label_dataset_from_log(
        [TradeOutcome("s", "X", "long", pd.Timestamp("2026-01-01", tz="UTC"), 1.0)],
        pd.DataFrame(),
    )
    assert len(frame) == 0 and summary["samples"] == 0
