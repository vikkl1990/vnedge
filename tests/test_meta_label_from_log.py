"""Log-backed meta-label dataset: exact-vector join, causal match, counters."""

from __future__ import annotations

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
            ["ts", "bar_ts", "strategy_id", "symbol", "side", "decision_id",
             "decision", "intent_key", "backfill"]
            + FEATURE_COLUMNS
        ),
    )


def test_join_uses_logged_vector_verbatim():
    log = _log([
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "decision": "fired",
         "bar_ts": "2026-01-01T12:00:00+00:00", "side": "long",
         "decision_id": "d1", "rsi14": 71.5, "atr_bps": 42.0},
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "decision": "pass",
         "bar_ts": "2026-01-01T12:30:00+00:00", "rsi14": 30.0},
    ])
    trade = TradeOutcome(
        strategy="s1", symbol="BTC/USDT:USDT", side="long",
        entry_ts=pd.Timestamp("2026-01-01T12:05:00+00:00"), net_usd=17.0,
        decision_id="d1", path_id="kernel_v1", performance_eligible=True,
    )
    frame, summary = build_meta_label_dataset_from_log([trade], log)
    assert summary["matched"] == 1 and summary["samples"] == 1
    assert frame["meta_label"].iloc[0] == 1.0
    # the EXACT logged numbers, not a re-derivation
    assert frame["rsi14"].iloc[0] == 71.5
    assert frame["atr_bps"].iloc[0] == 42.0


def test_only_fired_rows_match_exact_decision_id():
    log = _log([
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "decision": "pass",
         "bar_ts": "2026-01-01T12:00:00+00:00", "rsi14": 10.0},   # pass: ignored
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "decision": "fired",
         "bar_ts": "2026-01-01T13:00:00+00:00", "side": "long",
         "decision_id": "other", "rsi14": 55.0},
    ])
    trade = TradeOutcome(
        strategy="s1", symbol="BTC/USDT:USDT", side="long",
        entry_ts=pd.Timestamp("2026-01-01T12:30:00+00:00"), net_usd=-5.0,
        decision_id="wanted", path_id="kernel_v1", performance_eligible=True,
    )
    frame, summary = build_meta_label_dataset_from_log([trade], log)
    # no fired row at-or-before entry -> unmatched, counted, never guessed
    assert summary["matched"] == 0 and summary["dropped_no_log_row"] == 1
    assert len(frame) == 0


def test_time_proximity_cannot_substitute_for_decision_identity():
    log = _log([
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "decision": "fired",
         "bar_ts": "2026-01-01T00:00:00+00:00", "side": "long",
         "decision_id": "d-old", "rsi14": 60.0},
    ])
    trade = TradeOutcome(
        strategy="s1", symbol="BTC/USDT:USDT", side="long",
        entry_ts=pd.Timestamp("2026-01-01T00:00:01+00:00"), net_usd=1.0,
        decision_id="d-new", path_id="kernel_v1", performance_eligible=True,
    )
    _, summary = build_meta_label_dataset_from_log([trade], log)
    assert summary["matched"] == 0


def test_missing_contract_column_raises():
    partial = pd.DataFrame(
        [{"strategy_id": "s1", "symbol": "X", "decision": "fired",
          "bar_ts": "2026-01-01T00:00:00+00:00", "decision_id": "d1",
          "rsi14": 1.0}]
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


def _log_v2(rows: list[dict]) -> pd.DataFrame:
    base = {col: 0.0 for col in FEATURE_COLUMNS}
    cols = (
        ["ts", "bar_ts", "strategy_id", "symbol", "lane", "side",
         "decision_id", "decision", "backfill"] + FEATURE_COLUMNS
    )
    return pd.DataFrame([{**base, **r} for r in rows], columns=cols)


def test_backfill_rows_are_excluded():
    log = _log_v2([
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "lane": "L1", "side": "long",
         "decision": "fired", "backfill": True,
         "decision_id": "d1",
         "bar_ts": "2026-01-01T12:00:00+00:00", "rsi14": 71.0},
    ])
    trade = TradeOutcome("s1", "BTC/USDT:USDT", "long",
                         pd.Timestamp("2026-01-01T12:05:00+00:00"), 5.0, lane="L1",
                         decision_id="d1", path_id="kernel_v1", performance_eligible=True)
    _, summary = build_meta_label_dataset_from_log([trade], log)
    assert summary["matched"] == 0  # backfill reconstruction is not a live fire


def test_join_discriminates_on_exact_id_and_validates_identity():
    log = _log_v2([
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "lane": "L1", "side": "long",
         "decision": "fired", "backfill": False,
         "decision_id": "d1",
         "bar_ts": "2026-01-01T12:00:00+00:00", "rsi14": 60.0},
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "lane": "L2", "side": "short",
         "decision": "fired", "backfill": False,
         "decision_id": "d2",
         "bar_ts": "2026-01-01T12:00:00+00:00", "rsi14": 40.0},
    ])
    # a long trade on lane L1 must pick the L1/long row (rsi14=60), never L2/short
    trade = TradeOutcome("s1", "BTC/USDT:USDT", "long",
                         pd.Timestamp("2026-01-01T12:05:00+00:00"), 3.0, lane="L1",
                         decision_id="d1", path_id="kernel_v1", performance_eligible=True)
    frame, summary = build_meta_label_dataset_from_log([trade], log)
    assert summary["matched"] == 1
    assert frame["rsi14"].iloc[0] == 60.0
    # a short trade on L2 picks the other row
    trade2 = TradeOutcome("s1", "BTC/USDT:USDT", "short",
                          pd.Timestamp("2026-01-01T12:05:00+00:00"), -1.0, lane="L2",
                          decision_id="d2", path_id="kernel_v1", performance_eligible=True)
    frame2, _ = build_meta_label_dataset_from_log([trade2], log)
    assert frame2["rsi14"].iloc[0] == 40.0


def test_missing_or_ineligible_outcome_never_becomes_a_label():
    log = _log_v2([
        {"strategy_id": "s1", "symbol": "BTC/USDT:USDT", "lane": "L1",
         "side": "long", "decision": "fired", "backfill": False,
         "decision_id": "d1", "bar_ts": "2026-01-01T12:00:00+00:00"},
    ])
    missing = TradeOutcome(
        "s1", "BTC/USDT:USDT", "long",
        pd.Timestamp("2026-01-01T12:05:00+00:00"), 5.0, lane="L1",
    )
    observe_only = TradeOutcome(
        "s1", "BTC/USDT:USDT", "long",
        pd.Timestamp("2026-01-01T12:05:00+00:00"), 5.0, lane="L1",
        decision_id="d1", path_id="research_observe", performance_eligible=False,
    )
    _, summary = build_meta_label_dataset_from_log([missing, observe_only], log)
    assert summary["matched"] == 0
    assert summary["dropped_missing_decision_id"] == 1
    assert summary["dropped_ineligible_outcome"] == 1


def test_duplicate_feature_decision_id_is_refused():
    row = {
        "strategy_id": "s1", "symbol": "BTC/USDT:USDT", "lane": "L1",
        "side": "long", "decision": "fired", "backfill": False,
        "decision_id": "duplicate", "bar_ts": "2026-01-01T12:00:00+00:00",
    }
    log = _log_v2([row, row])
    try:
        build_meta_label_dataset_from_log([], log)
        raise AssertionError("duplicate decision identity must fail closed")
    except ValueError as exc:
        assert "duplicate decision_id" in str(exc)
