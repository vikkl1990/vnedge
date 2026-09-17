import pytest
import pandas as pd

from vnedge.research.scanner_signal_gap import summarize
from vnedge.strategy.structure_bos_15m_trigger_v2 import _complete_hour_frame


def evaluation(hour, **kwargs):
    return {"kind": "lane_eval", "payload": {
        "strategy_id": "htf", "symbol": "BTCUSD",
        "bar_ts": f"2026-09-16T{hour:02}:00:00+00:00",
        "fired": False, "primary_failed_gate": "regime_flat",
        "all_failed_gates": ["regime_flat", "gap_parent"], **kwargs,
    }}


def test_trace_deduplicates_bars_and_excludes_backfill_and_heartbeats():
    report = summarize([
        evaluation(1), evaluation(2), evaluation(2),
        evaluation(3, backfill=True, fired=True),
        {"kind": "paper_lane_heartbeat", "payload": {"fired": True}},
        {"kind": "decision_armed", "payload": {}},
        evaluation(4),
    ], bars=2)
    assert report["evaluations"] == 2
    assert report["signals"] == 0
    assert report["primary_gate_counts"] == {"regime_flat": 2}
    assert report["all_gate_counts"] == {"regime_flat": 2, "gap_parent": 2}
    assert report["first_bar"] == "2026-09-16T02:00:00+00:00"
    assert report["whole_journal_event_counts"]["decision_armed"] == 1
    assert not report["can_trade"]


def test_trace_preserves_unexplained_silence_and_signal_without_claiming_arm():
    report = summarize([evaluation(1, primary_failed_gate=None, all_failed_gates=[]),
                        evaluation(2, fired=True, primary_failed_gate=None, all_failed_gates=[])])
    assert report["primary_gate_counts"] == {"unexplained_no_signal": 1, "signal": 1}
    assert report["signals"] == 1
    assert "decision_armed" not in report["whole_journal_event_counts"]
    with pytest.raises(ValueError):
        summarize([], bars=0)


@pytest.mark.parametrize("flag", ["is_closed", "coverage_ok"])
@pytest.mark.parametrize("value", [False, None])
def test_parent_refuses_forming_or_uncovered_child(flag, value):
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2026-09-16", periods=4, freq="15min", tz="UTC"),
        "open": [100]*4, "high": [102]*4, "low": [99]*4,
        "close": [101]*4, "volume": [1]*4, "data_quality": ["ok"]*4,
        flag: [True, True, value, True],
    })
    assert _complete_hour_frame(frame).iloc[0].data_quality == "gap"
    assert _complete_hour_frame(frame.iloc[:3]).empty
