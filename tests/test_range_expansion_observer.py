from datetime import UTC, datetime, timedelta

import pandas as pd

from vnedge.strategy.range_expansion_observer import RangeExpansionObserver


def _frame() -> pd.DataFrame:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = []
    for index in range(30):
        rows.append({
            "timestamp": start + timedelta(hours=index),
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "volume": 100.0,
        })
    rows[25].update(
        {"open": 100.0, "high": 104.0, "low": 99.8, "close": 103.0, "volume": 200.0}
    )
    return pd.DataFrame(rows)


def test_first_12h_range_break_emits_exploratory_long() -> None:
    strategy = RangeExpansionObserver()
    prepared = strategy.prepare(_frame())
    signal = strategy.signal(prepared, 25)
    assert signal is not None
    assert signal.side == "long"
    assert signal.stop_price < 103.0
    assert signal.take_profit_price is None
    assert "12h_break" in signal.reason


def test_future_rows_do_not_change_prior_range_break() -> None:
    strategy = RangeExpansionObserver()
    full = _frame()
    prefix_signal = strategy.signal(strategy.prepare(full.iloc[:26]), 25)
    full_signal = strategy.signal(strategy.prepare(full), 25)
    assert prefix_signal == full_signal


def test_thin_volume_break_is_rejected() -> None:
    frame = _frame()
    frame.loc[25, "volume"] = 100.0
    strategy = RangeExpansionObserver()
    assert strategy.signal(strategy.prepare(frame), 25) is None
