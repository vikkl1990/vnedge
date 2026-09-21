import numpy as np
import pandas as pd
import pytest

from vnedge.research.vela_family import (
    Config,
    Position,
    a_signal,
    b_session,
    daily_features,
    exit_rule,
    replay,
    settle,
)


def daily(n=65):
    return pd.DataFrame({"open": 1000., "high": 1100., "low": 900., "close": 1000.,
                         "volume": 100., "complete": True},
                        index=pd.date_range("2026-07-01", periods=n, freq="D", tz="UTC"))


def intraday(day):
    return pd.DataFrame({"open": 1000., "high": 1050., "low": 950., "close": 1000.,
                         "volume": 100., "complete": True},
                        index=pd.date_range(day, periods=96, freq="15min"))


@pytest.mark.parametrize("side", [1, -1])
def test_exit_order_tie_stop_target_then_kill(side):
    p = Position("A", side, 1000, 1000-side*100, 1000+side*200, 1000, 1, "t")
    assert exit_rule(p, pd.Series({"low": 700, "high": 1300, "close": 1000}), kill=True) == (p.stop, "stop_and_target")
    assert exit_rule(p, pd.Series({"low": 990, "high": 1010, "close": 1000-side*5}), kill=True) == (1000-side*5, "kill")
    assert exit_rule(p, pd.Series({"low": 990, "high": 1010, "close": 1000}), kill=True) is None


@pytest.mark.parametrize("n", [1, 10, 20])
def test_range_causality_and_boundary_equality(n):
    b = daily()
    cfg = Config(n)
    f = daily_features(b, cfg)
    b.loc[b.index[60], "high"] = 9999
    changed = daily_features(b, cfg)
    assert changed.yh.iloc[60] == f.yh.iloc[60]
    pd.testing.assert_frame_equal(changed.iloc[:60], daily_features(b.iloc[:60], cfg))
    row = f.iloc[60].copy()
    row["close"] = row.yh
    assert a_signal(row, cfg)["action"] == "WAIT"
    row["close"] = row.yh+1
    assert a_signal(row, cfg)["action"] == "SIGNAL"
    row["complete"] = False
    assert a_signal(row, cfg)["action"] == "WATCH"


def test_manage_blocks_second_entry_and_no_entry_bar_stop():
    b = daily()
    b.loc[b.index[50], ["close", "high", "low"]] = [1200, 1250, 800]
    # Entry day wick would stop it if lookahead management were incorrectly run.
    b.loc[b.index[51], ["close", "high", "low"]] = [1500, 1600, 850]
    result = replay(b, pd.DataFrame(), Config(), b.index[50], b.index[52])
    assert [e["A"] for e in result["events"]] == ["SIGNAL", "EXIT"]
    assert len(result["trades"]) == 1
    assert result["trades"][0]["entry_ts"] == b.index[51].isoformat()
    assert result["open_A"] is None


@pytest.mark.parametrize("open_price,expected", [(1000, "CANCEL_PENDING"), (1150, "MANAGE")])
def test_pending_open_gate_and_recomputed_target(open_price, expected):
    b = daily()
    b.loc[b.index[50], ["close", "high"]] = [1200, 1250]
    b.loc[b.index[51], ["open", "low", "high", "close"]] = [open_price, 1120, 1250, 1200]
    result = replay(b, pd.DataFrame(), Config(fill="next_open"), b.index[50], b.index[52])
    assert result["events"][-1]["A"] == expected
    if open_price == 1150:
        assert result["open_A"]["target"] == 1350
        assert result["open_A"]["stop"] == 900
    else:
        assert result["open_A"] is None and result["pending_A"] is None


def test_b_box_excludes_break_bar_and_manage_starts_later():
    day = pd.Timestamp("2026-08-01", tz="UTC")
    b = intraday(day)
    at = day+pd.Timedelta(hours=8, minutes=30)
    b.loc[at, ["close", "high", "low"]] = [1060, 1400, 800]
    b.loc[at+pd.Timedelta(minutes=15), ["high", "low"]] = [1400, 800]
    trades, action, unresolved = b_session(b, day, Config(b_delta="off"))
    assert action == "DONE_TODAY" and unresolved is None
    assert trades[0]["entry_ts"] == (at+pd.Timedelta(minutes=15)).isoformat()
    assert trades[0]["stop"] == 950 and trades[0]["target"] == 1170
    assert trades[0]["reason"] == "stop_and_target"


@pytest.mark.parametrize("mode", ["cvd", "candle_proxy"])
def test_delta_rejection_consumes_only_attempt(mode):
    day = pd.Timestamp("2026-08-01", tz="UTC")
    b = intraday(day)
    b.loc[day+pd.Timedelta(hours=9), "close"] = 1060
    b.loc[day+pd.Timedelta(hours=10), "close"] = 940
    trades, action, _ = b_session(b, day, Config(b_delta=mode))
    assert not trades and action == "DELTA_REJECT"


def test_b_eod_and_missing_box():
    day = pd.Timestamp("2026-08-01", tz="UTC")
    b = intraday(day)
    b.loc[day+pd.Timedelta(hours=9):, ["low", "high", "close"]] = [1000, 1100, 1060]
    trades, _, _ = b_session(b, day, Config(b_delta="off"))
    assert trades[0]["reason"] == "eod"
    assert trades[0]["exit_ts"] == (day+pd.Timedelta(days=1)).isoformat()
    b.loc[day+pd.Timedelta(hours=7), "complete"] = False
    assert b_session(b, day, Config(b_delta="off"))[1] == "MISSING_BOX"


def test_b_uses_prior_a_state_not_todays_close():
    b = daily()
    day = b.index[50]
    b.loc[day, ["close", "high"]] = [1200, 1250]
    intra = pd.concat([intraday(day), intraday(day+pd.Timedelta(days=1))])
    intra.loc[day+pd.Timedelta(hours=9), ["close", "high"]] = [1060, 1070]
    result = replay(b, intra, Config(b_delta="off"), day, day+pd.Timedelta(days=2))
    assert result["events"][0]["A_at_07"] == "FLAT"
    assert result["events"][0]["B"] == "DONE_TODAY"
    assert result["events"][1]["B"] == "A_NOT_FLAT"
    assert len([t for t in result["trades"] if t["book"] == "B"]) == 1


def test_gap_quarantines_open_a_instead_of_clearing_occupancy():
    b = daily()
    b.loc[b.index[50], ["close", "high"]] = [1200, 1250]
    b.loc[b.index[51], "complete"] = False
    result = replay(b, pd.DataFrame(), Config(), b.index[50], b.index[54])
    assert result["unresolved_A"] and result["open_A"]
    assert result["events"][-1]["A"] == "QUARANTINED"
    assert not result["trades"]


def test_costs_on_both_fills_and_notional_sizing():
    p = Position("A", 1, 10000, 9000, 11000, 9500, .1, "t")
    t = settle(p, 11000, pd.Timestamp("2026-08-01", tz="UTC"), "target", Config())
    assert t["gross_usd"] == 100
    assert t["fees_usd"] == pytest.approx(1.239)
    assert t["slippage_usd"] == pytest.approx(.1)


def test_missing_comp_cannot_signal():
    row = daily_features(daily(), Config()).iloc[10]
    assert np.isnan(row.comp)
    assert a_signal(row, Config())["action"] == "WARMUP"
