from datetime import UTC, datetime

import pandas as pd

from vnedge.strategy.range_expansion_observer_v2 import RangeExpansionObserverV2


def _frame() -> pd.DataFrame:
    timestamps = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=600, freq="1h")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 100.0,
            "high": 100.2,
            "low": 99.8,
            "close": 100.0,
            "volume": 100.0,
        }
    )


def _next_index_at_hour(frame: pd.DataFrame, hour: int) -> int:
    hours = pd.to_datetime(frame["timestamp"], utc=True).dt.hour
    return int(frame.index[(frame.index > 500) & (hours == hour)][0])


def test_v2_requires_session_and_same_hour_expansion_and_emits_target():
    strategy = RangeExpansionObserverV2()
    active = _frame()
    active_index = _next_index_at_hour(active, 13)
    active.loc[active_index, ["open", "high", "low", "close", "volume"]] = [
        100.0,
        103.2,
        99.8,
        103.0,
        200.0,
    ]
    prepared = strategy.prepare(active)
    signal = strategy.signal(prepared, active_index)
    assert signal is not None
    assert signal.side == "long"
    assert signal.take_profit_price is not None
    assert signal.take_profit_price > float(prepared.iloc[active_index]["close"])
    assert prepared.iloc[active_index]["rex_session_ok"] == 1.0
    assert prepared.iloc[active_index]["rex_hour_range_ratio"] >= 1.2

    quiet = _frame()
    quiet_index = _next_index_at_hour(quiet, 11)
    quiet.loc[quiet_index, ["open", "high", "low", "close", "volume"]] = [
        100.0,
        103.2,
        99.8,
        103.0,
        200.0,
    ]
    quiet_prepared = strategy.prepare(quiet)
    assert quiet_prepared.iloc[quiet_index]["rex_session_ok"] == 0.0
    assert strategy.signal(quiet_prepared, quiet_index) is None
