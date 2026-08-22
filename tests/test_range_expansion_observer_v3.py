from __future__ import annotations

import pandas as pd

from vnedge.strategy.range_expansion_observer_v3 import RangeExpansionObserverV3


def _history() -> pd.DataFrame:
    ts = pd.date_range(
        end="2026-08-22 12:15", periods=21 * 24 * 4 + 2, freq="15min", tz="UTC"
    )
    frame = pd.DataFrame(
        {
            "timestamp": ts,
            "open": 100.0,
            "high": 100.1,
            "low": 99.9,
            "close": 100.0,
            "volume": 100.0,
            "data_quality": "ok",
        }
    )
    # The final partial hour is inside the frozen 12:00-16:00 UTC block.
    final_day = frame["timestamp"].dt.date.max()
    candidates = frame.index[
        (frame["timestamp"].dt.date == final_day)
        & (frame["timestamp"].dt.hour == 12)
    ]
    final = int(candidates[1])
    frame = frame.iloc[: final + 1].copy()
    frame.loc[final, ["open", "high", "low", "close", "volume"]] = [
        100.0,
        102.1,
        99.9,
        102.0,
        200.0,
    ]
    return frame.reset_index(drop=True)


def test_v3_confirms_forming_hour_on_second_15m_close() -> None:
    strategy = RangeExpansionObserverV3()
    prepared = strategy.prepare(_history())
    row = prepared.iloc[-1]

    assert row["rex3_session_ok"] == 1.0
    assert row["rex3_expansion_ok"] == 1.0
    assert row["rex3_volume_ok"] == 1.0
    assert row["rex3_fire_long"] == 1.0
    signal = strategy.signal(prepared, len(prepared) - 1)
    assert signal is not None
    assert signal.side == "long"
    assert "confirmation=15m" in signal.reason


def test_v3_does_not_fire_without_volume_confirmation() -> None:
    strategy = RangeExpansionObserverV3()
    candles = _history()
    candles.loc[candles.index[-1], "volume"] = 100.0

    row = strategy.prepare(candles).iloc[-1]

    assert row["rex3_volume_ok"] == 0.0
    assert row["rex3_fire_long"] == 0.0
