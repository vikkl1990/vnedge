"""Synthetic exit-path verification; no market-data fitting."""
import importlib.util
import runpy
from pathlib import Path

import pandas as pd
import pytest

from vnedge.research.range_break_retest import RangeBreakRetestResearch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location("range_retest_replay", HERE / "replay.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)
fixture = runpy.run_path(str(ROOT / "tests/test_range_break_retest_research.py"))["fixture"]


def setup(short: bool = False) -> tuple:
    candidate = RangeBreakRetestResearch("BTCUSD").evaluate(fixture(short=short), 21).candidate
    now = pd.Timestamp("2026-09-12T01:50:00Z")
    entry = candidate.reference_entry
    minutes = pd.DataFrame({"open": entry, "high": entry + 0.1, "low": entry - 0.1,
                            "close": entry + 0.05, "eligible": True},
                           index=pd.date_range(now, periods=15, freq="min"))
    return candidate, now, minutes


@pytest.mark.parametrize("short", [False, True])
def test_timeout_is_fifteenth_minute_close_with_one_cost_charge(short: bool) -> None:
    candidate, now, minutes = setup(short)
    event = replay.simulate(candidate, now, minutes)
    direction = -1 if short else 1
    expected = direction * (minutes.iloc[-1].close / candidate.reference_entry - 1) * 10000
    assert event["status"] == "measured"
    assert event["entry_time"] == now.isoformat()
    assert event["exit_reason"] == "timeout"
    assert pd.Timestamp(event["exit_known_by"]) == now + pd.Timedelta(minutes=15)
    assert event["gross_bps"] == pytest.approx(expected)
    assert event["net_bps"] == pytest.approx(expected - 17.8)
    assert not event["can_trade"] and not event["performance_eligible"]


@pytest.mark.parametrize("short", [False, True])
def test_same_minute_stop_and_target_is_stop_first(short: bool) -> None:
    candidate, now, minutes = setup(short)
    minutes.iloc[0, minutes.columns.get_loc("low")] = 93
    minutes.iloc[0, minutes.columns.get_loc("high")] = 107
    event = replay.simulate(candidate, now, minutes)
    assert event["exit_reason"] == "stop_tie"
    assert event["exit_proxy"] == candidate.signal.stop_price
    assert pd.Timestamp(event["reserved_until"]) == now + pd.Timedelta(minutes=1)


def test_adverse_gap_uses_worse_open_never_stop_level() -> None:
    candidate, now, minutes = setup()
    minutes.iloc[1, :4] = [101.5, 101.7, 101.3, 101.6]
    event = replay.simulate(candidate, now, minutes)
    assert event["exit_reason"] == "stop_gap"
    assert event["exit_proxy"] == 101.5
    assert event["reserved_until"] == (now + pd.Timedelta(minutes=1)).isoformat()


def test_favorable_gap_never_claims_target_improvement() -> None:
    candidate, now, minutes = setup()
    minutes.iloc[1, :4] = [106.5, 107, 106.3, 106.6]
    event = replay.simulate(candidate, now, minutes)
    assert event["exit_reason"] == "target_gap"
    assert event["exit_proxy"] == 106


def test_censor_before_exit_but_not_after_known_exit() -> None:
    candidate, now, minutes = setup()
    minutes.iloc[5, minutes.columns.get_loc("eligible")] = False
    event = replay.simulate(candidate, now, minutes)
    assert event["status"] == "censored_missing_path"
    assert event["reserved_until"] == (now + pd.Timedelta(minutes=15)).isoformat()
    minutes.iloc[0, minutes.columns.get_loc("high")] = 106.1
    event = replay.simulate(candidate, now, minutes)
    assert event["status"] == "measured" and event["exit_reason"] == "target"


def test_missing_entry_reserves_horizon_without_fabricated_price() -> None:
    candidate, now, minutes = setup()
    event = replay.simulate(candidate, now, minutes.iloc[1:])
    assert event["status"] == "censored_missing_entry"
    assert "entry_proxy" not in event
    assert event["reserved_until"] == (now + pd.Timedelta(minutes=15)).isoformat()


def test_entry_gap_rechecks_reject_without_moving_stop_or_target() -> None:
    candidate, now, minutes = setup()
    minutes.iloc[0, :4] = [105.5, 105.7, 105.3, 105.6]
    event = replay.simulate(candidate, now, minutes)
    assert event["status"] == "entry_rejected"
    assert "entry_gap_chase_cap" in event["failed_gates"]
    assert event["stop_price"] == candidate.signal.stop_price
    assert event["target_price"] == candidate.signal.take_profit_price
    assert event["reserved_until"] == now.isoformat()
    assert "entry_gap_lost_level" in replay.entry_failures(candidate, 101.9)
