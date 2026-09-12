"""Causality, missing-data and arithmetic checks for the fixed screen."""

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SPEC = importlib.util.spec_from_file_location("burst_screen", Path(__file__).with_name("screen.py"))
screen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(screen)


def minutes():
    index = pd.date_range(screen.START, periods=180, freq="min")
    frame = pd.DataFrame({"open": 100., "high": 100.3, "low": 99.9, "close": 100.,
                          "quote_volume": 100., "eligible": True}, index=index)
    # Thirteenth parent, after precisely twelve complete reference parents.
    frame.loc[index[60:65], "quote_volume"] = 200.
    frame.loc[index[64], "close"] = 100.3
    return frame


def test_setup_uses_only_closed_prefix():
    frame = minutes()
    full = screen.five_minute_frame(frame)
    prefix = screen.five_minute_frame(frame.iloc[:65])
    assert prefix.iloc[-1].setup
    assert prefix.iloc[-1].notional_ratio == 2
    assert full.loc[prefix.index[-1], "setup"] == prefix.iloc[-1].setup
    changed = frame.copy()
    changed.loc[changed.index[65:], "quote_volume"] = 1e12
    assert screen.five_minute_frame(changed).iloc[12].setup


@pytest.mark.parametrize("bad_index", [0, 62])
def test_bad_reference_or_signal_child_blocks_setup(bad_index):
    frame = minutes()
    frame.iloc[bad_index, frame.columns.get_loc("eligible")] = False
    assert not screen.five_minute_frame(frame).iloc[12].setup


def test_missing_child_does_not_make_complete_parent():
    frame = minutes().drop(minutes().index[62])
    assert not screen.five_minute_frame(frame).iloc[12].eligible


def test_delay_horizon_cost_and_competing_sides():
    frame = minutes()
    frame.loc[screen.START + 81 * screen.MINUTE, "open"] = 100.5
    bars = screen.five_minute_frame(frame)
    cont, counts = screen.events(frame, bars, "continuation")
    rev, _ = screen.events(frame, bars, "reversal")
    assert counts["measured"] == 1
    assert pd.Timestamp(cont[0]["entry_time"]) == screen.START + 66 * screen.MINUTE
    assert pd.Timestamp(cont[0]["exit_time"]) == screen.START + 81 * screen.MINUTE
    assert cont[0]["gross_bps"] == pytest.approx(50)
    assert rev[0]["gross_bps"] == pytest.approx(-50)
    result = screen.summarize(cont, 17.8)
    assert result["net_mean_bps"] == pytest.approx(32.2)
    assert result["verdict"] == "INSUFFICIENT_SAMPLE"
    assert not result["can_trade"] and not result["can_promote"]


def test_censored_event_reserves_hold_and_never_becomes_zero_return():
    frame = minutes()
    bars = screen.five_minute_frame(frame)
    bars.loc[bars.index[13], "setup"] = True
    frame.iloc[70, frame.columns.get_loc("eligible")] = False
    result, counts = screen.events(frame, bars, "continuation")
    assert counts == {"setups": 2, "censored": 1, "overlapping_skipped": 1}
    assert len(result) == 1
    assert "gross_bps" not in result[0]
    assert screen.summarize(result, 17.8)["verdict"] == "UNMEASURED"


def test_bootstrap_and_metrics_repeatable():
    record = {"status": "measured", "decision_open": screen.START.isoformat(), "gross_bps": 10.}
    a = screen.summarize([record] * 30, 17.8)
    assert a == screen.summarize([record] * 30, 17.8)
    assert a["verdict"] == "UNSUPPORTED_AT_MODELED_COST"
    assert np.isclose(a["profit_factor"], 0)
    assert np.isclose(a["drawdown_cumulative_net_bps"], 234)
