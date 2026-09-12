import importlib.util
from pathlib import Path

import pandas as pd
import pytest

SPEC = importlib.util.spec_from_file_location("quant_harness", Path(__file__).with_name("run_replay.py"))
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def minute_frame():
    ts = pd.date_range(harness.START, periods=120, freq="min")
    return pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100.,
                         "volume": "2", "eligible": True}, index=ts)


def official():
    return pd.DataFrame({"timestamp": [harness.START], "open": [100.], "high": [101.],
                         "low": [99.], "close": [100.], "volume": [100.]})


def test_children_are_summed_numerically_and_missing_child_rejected():
    minutes = minute_frame()
    minutes.iloc[2, minutes.columns.get_loc("eligible")] = False
    frames = harness.prepare_frames(minutes, official())
    assert harness.START not in frames["5m"].index
    assert frames["5m"].iloc[0].volume == 10
    assert frames["15m"].iloc[0].volume == 30


def test_all_frames_are_filtered_on_close_not_open():
    frames = harness.prepare_frames(minute_frame(), official())
    now = harness.START + pd.Timedelta(minutes=65)
    view = harness.visible_frames(frames, now)
    assert view["5m"].timestamp.iloc[-1] == now - pd.Timedelta(minutes=5)
    assert view["1h"].timestamp.iloc[-1] == harness.START
    assert view["4h"].empty
    for tf, frame in view.items():
        assert (frame.timestamp + pd.Timedelta(tf) <= now).all()


def test_markout_delay_and_censor():
    minutes = minute_frame()
    now = harness.START + pd.Timedelta(minutes=60)
    minutes.loc[now + pd.Timedelta(minutes=16), "open"] = 101.
    signal = {"side": "long", "metadata": {"setup_type": "test"}}
    result = harness.measured_event(signal, now, minutes)
    assert result["gross_bps"] == pytest.approx(100)
    assert pd.Timestamp(result["entry_time"]) == now + pd.Timedelta(minutes=1)
    minutes.loc[now + pd.Timedelta(minutes=4), "eligible"] = False
    assert harness.measured_event(signal, now, minutes)["status"].startswith("censored")


def test_frozen_clock_is_historical_not_current_wall_time():
    assert harness.ReplayDateTime.now(harness.UTC).isoformat() == harness.START.isoformat()
