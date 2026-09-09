from datetime import UTC, datetime, timedelta
import json

import pandas as pd
import pytest

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.research.data_readiness import describe_research_input


def frame(n=10):
    rows = []
    for i in range(n):
        opened = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(hours=i)
        row = dict(timestamp=opened, open=100., high=102., low=99., close=101.,
                   volume=1., quote_volume=101., trade_count=1, candle_source="canonical_tick_lake",
                   is_closed=True, coverage_ok=True, data_quality="ok",
                   exchange="delta_india", symbol="BTC/USD:USD", timeframe="1h")
        row["content_sha256"] = bar_content_sha256(row, open_time=opened,
            close_time=opened+timedelta(hours=1), source="canonical_tick_lake")
        rows.append(row)
    return pd.DataFrame(rows)


def describe(data, **kwargs):
    return describe_research_input(data, exchange="delta_india", symbol="BTC/USD:USD",
        timeframe="1h", required_bars=12, exact_volume=False,
        now=kwargs.get("now", datetime(2026, 9, 9, tzinfo=UTC)))


def test_short_history_is_not_reported_as_internal_gaps():
    data = frame()
    before = data.copy(deep=True)
    result = describe(data)
    pd.testing.assert_frame_equal(data, before)
    assert result["row_shortfall"] == 2
    assert result["verified_unique_bars"] == 10
    assert result["missing_internal_bars"] == 0
    assert result["longest_contiguous_bars"] == result["latest_contiguous_bars"] == 10
    assert result["historical_coverage"] == "outside_frame_unknown"
    assert result["can_trade"] is result["repair_authorized"] is False
    json.dumps(result, allow_nan=False)


def test_absent_slots_and_present_bad_proof_remain_separate():
    data = frame().drop(index=[2, 3])
    data.loc[7, "coverage_ok"] = False
    result = describe(data)
    assert result["stored_rows"] == 8
    assert result["verified_unique_bars"] == 7
    assert result["missing_internal_bars"] == 2
    assert result["invalid_row_counts"] == {"coverage_unproven": 1}
    assert result["longest_contiguous_bars"] == 3
    assert result["latest_contiguous_bars"] == 2
    assert result["gap_ranges"] == [{"from_open": "2026-09-01T02:00:00+00:00",
                                    "to_open": "2026-09-01T03:00:00+00:00", "missing_bars": 2}]


def test_duplicate_slots_are_ambiguous_not_extra_history():
    data = pd.concat([frame().iloc[:5], frame().iloc[4:]], ignore_index=True)
    result = describe(data)
    assert result["duplicate_slots"] == 1
    assert result["verified_unique_bars"] == 9
    assert result["longest_contiguous_bars"] == 5
    assert result["missing_internal_bars"] == 0
    assert "ambiguous_row_order" in result["blockers"]


def test_reversed_rows_are_visible_even_when_all_hashes_are_valid():
    result = describe(frame().iloc[::-1])
    assert result["out_of_order_rows"] == 9
    assert "ambiguous_row_order" in result["blockers"]


@pytest.mark.parametrize("key,value,reason", [
    ("is_closed", False, "decision_row_not_closed"),
    ("content_sha256", "a"*64, "decision_row_content_hash_mismatch"),
    ("symbol", "ETH/USD:USD", "symbol_identity_missing_or_mismatch"),
    ("timestamp", None, "timestamp_invalid"),
])
def test_no_proof_upgrade(key, value, reason):
    data = frame()
    data.loc[9, key] = value
    result = describe(data)
    assert result["invalid_row_counts"][reason] == 1
    assert result["verified_unique_bars"] == 9


def test_invalid_last_row_does_not_invent_a_continuous_tail():
    data = frame()
    data.loc[9, "coverage_ok"] = False
    assert describe(data)["latest_contiguous_bars"] == 0


def test_empty_and_future_frames_have_no_verified_history():
    assert describe(pd.DataFrame())["verified_unique_bars"] == 0
    result = describe(frame(), now=datetime(2025, 1, 1, tzinfo=UTC))
    assert result["verified_unique_bars"] == 0
    assert result["invalid_row_counts"] == {"future_bar": 10}


def test_gap_ranges_are_bounded_but_counts_are_complete():
    result = describe(frame(100).iloc[::2])
    assert result["missing_internal_bars"] == 49
    assert result["gap_range_count"] == 49
    assert len(result["gap_ranges"]) == 32
    assert result["gap_ranges_truncated"] is True
