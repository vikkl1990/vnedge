from datetime import UTC, datetime, timedelta

import pandas as pd

from vnedge.strategy.research_scanners import (
    NEW_RESEARCH_SCANNERS,
    TickAcceptedBreakoutV1,
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


def test_new_scanners_are_explicitly_shadow_only_and_never_capital():
    for scanner in NEW_RESEARCH_SCANNERS:
        assert scanner.strategy_id in STRATEGIES
        assert scanner.strategy_id in RESEARCH_ONLY
        assert scanner.strategy_id in SHADOW_OBSERVE
        assert scanner.strategy_id not in CAPITAL_APPROVED
        assert is_capital_eligible(scanner.strategy_id) is False


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
