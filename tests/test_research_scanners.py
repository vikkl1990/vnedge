from datetime import UTC, datetime, timedelta

import pandas as pd

from vnedge.strategy.regime_router import regime_router_warmup_bars
from vnedge.strategy.research_scanners import (
    NEW_RESEARCH_SCANNERS,
    SHADOW_RESEARCH_SCANNERS,
    LiquiditySweepReversal15mV1,
    TickAcceptedBreakoutV1,
    TrendSqueezeContinuation1hV1,
)
from vnedge.strategy.strategy_registry import (
    CAPITAL_APPROVED,
    RESEARCH_ONLY,
    SHADOW_OBSERVE,
    STRATEGIES,
    is_capital_eligible,
)


def _candles(count: int = 800) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    close = [100.0 + index * 0.02 + ((index % 9) - 4) * 0.03 for index in range(count)]
    return pd.DataFrame(
        {
            "timestamp": [start + timedelta(minutes=15 * index) for index in range(count)],
            "open": [value - 0.02 for value in close],
            "high": [value + 0.10 for value in close],
            "low": [value - 0.10 for value in close],
            "close": close,
            "volume": [10.0 + index % 7 for index in range(count)],
            "quote_volume": [(10.0 + index % 7) * close[index] for index in range(count)],
            "trade_count": [20 for _ in range(count)],
            "data_quality": ["ok" for _ in range(count)],
            "is_closed": [True for _ in range(count)],
        }
    )


def test_new_scanners_are_registered_research_only_and_never_capital():
    for scanner in NEW_RESEARCH_SCANNERS:
        assert scanner.strategy_id in STRATEGIES
        assert scanner.strategy_id in RESEARCH_ONLY
        assert scanner.strategy_id not in CAPITAL_APPROVED
        assert is_capital_eligible(scanner.strategy_id) is False


def test_scanner_warmup_contract_covers_shared_regime_router():
    required = regime_router_warmup_bars()

    for scanner in NEW_RESEARCH_SCANNERS:
        assert scanner.warmup_bars >= required


def test_shadow_permission_is_narrower_than_research_registration():
    permitted = {scanner.strategy_id for scanner in SHADOW_RESEARCH_SCANNERS}
    assert permitted <= SHADOW_OBSERVE
    assert LiquiditySweepReversal15mV1.strategy_id not in SHADOW_OBSERVE
    assert TrendSqueezeContinuation1hV1.strategy_id not in SHADOW_OBSERVE


def test_all_new_scanners_prepare_and_explain_current_bar():
    candles = _candles()
    for scanner_class in NEW_RESEARCH_SCANNERS:
        scanner = scanner_class()
        prepared = scanner.prepare(candles)
        report = scanner.evaluation_diagnostics(prepared, len(prepared) - 1)
        assert len(prepared) == len(candles)
        assert isinstance(report["all_failed_gates"], list)
        assert isinstance(report["features"], dict)
        assert scanner.signal(prepared, scanner.warmup_bars - 1) is None


def test_new_scanners_treat_unknown_closed_state_as_not_eligible():
    candles = _candles()
    candles["is_closed"] = candles["is_closed"].astype("boolean")
    candles.loc[len(candles) - 1, "is_closed"] = pd.NA

    for scanner_class in NEW_RESEARCH_SCANNERS:
        prepared = scanner_class().prepare(candles)
        assert prepared.iloc[-1]["rs_quality_ok"] == 0.0


def test_new_scanner_features_are_prefix_causal():
    candles = _candles()
    changed = candles.copy()
    changed.loc[len(changed) - 1, ["high", "low", "close", "volume", "quote_volume"]] = [
        999.0, 1.0, 500.0, 9999.0, 4_999_500.0
    ]
    for scanner_class in NEW_RESEARCH_SCANNERS:
        left = scanner_class().prepare(candles).iloc[:-1].reset_index(drop=True)
        right = scanner_class().prepare(changed).iloc[:-1].reset_index(drop=True)
        pd.testing.assert_frame_equal(left, right)


def test_tick_acceptance_requires_time_samples_and_monotonic_ticks():
    scanner = TickAcceptedBreakoutV1()
    candles = _candles()
    trend = pd.Series([100.0 + index * 0.2 for index in range(len(candles))])
    candles["close"] = trend
    candles["open"] = trend - 0.05
    candles["high"] = trend + 0.10
    candles["low"] = trend - 0.10
    candles["quote_volume"] = candles["volume"] * candles["close"]
    prepared = scanner.prepare(candles)
    ready = prepared.index[prepared["tab_arm_ready"].eq(1)]
    assert len(ready) > 0
    index = int(ready[-1])
    scanner.signal(prepared, index)
    level = float(prepared.iloc[index]["tab_long_level"])
    t0 = datetime(2026, 1, 2, tzinfo=UTC)
    assert scanner.observe_tick(ts=t0, price=level + 1) is None
    assert scanner.observe_tick(ts=t0 + timedelta(seconds=1), price=level + 1) is None
    intent = scanner.observe_tick(ts=t0 + timedelta(seconds=3), price=level + 1)
    assert intent is not None
    assert intent.side == "long"
    assert "virtual_only" in intent.reason


def test_trend_squeeze_requires_completed_compression_then_release():
    scanner = TrendSqueezeContinuation1hV1()
    count = 140
    start = datetime(2026, 1, 1, tzinfo=UTC)
    close = pd.Series([100.0 + index * 0.02 for index in range(count)])
    candles = pd.DataFrame(
        {
            "timestamp": [start + timedelta(hours=index) for index in range(count)],
            "open": close - 0.01,
            "high": close + pd.Series([0.35] * 80 + [0.10] * (count - 80)),
            "low": close - pd.Series([0.35] * 80 + [0.10] * (count - 80)),
            "close": close,
            "volume": [10.0] * count,
            "quote_volume": close * 10.0,
            "trade_count": [20] * count,
            "data_quality": ["ok"] * count,
            "is_closed": [True] * count,
        }
    )
    candles.loc[count - 1, ["open", "high", "low", "close", "volume"]] = [
        float(close.iloc[-2]),
        float(close.iloc[-2] + 3.2),
        float(close.iloc[-2] - 0.05),
        float(close.iloc[-2] + 3.0),
        30.0,
    ]
    candles.loc[count - 1, "quote_volume"] = (
        candles.loc[count - 1, "close"] * candles.loc[count - 1, "volume"]
    )

    prepared = scanner.prepare(candles)
    row = prepared.iloc[-1]

    assert row["tsc1h_compression_ready"] == 1
    assert row["tsc1h_release"] == 1
    assert row["tsc1h_fire"] == 1
    intent = scanner.signal(prepared, len(prepared) - 1)
    assert intent is not None and intent.side == "long"
    assert intent.stop_price < float(row["close"]) < intent.take_profit_price


def test_scanner_diagnostics_include_non_binding_near_miss_distances():
    scanner = TrendSqueezeContinuation1hV1()
    prepared = scanner.prepare(_candles())
    diagnostics = scanner.evaluation_diagnostics(prepared, len(prepared) - 1)

    assert diagnostics["distance_to_threshold"]
    assert all(value >= 0 for value in diagnostics["distance_to_threshold"].values())
