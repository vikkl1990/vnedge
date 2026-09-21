import numpy as np
import pandas as pd
import pytest

from vnedge.research.price_action_candidates import CANDIDATES, replay, scan, summarize


def bars(n=100):
    return pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100.2,
                             "volume": 100., "notional": 10000., "eligible": True},
                        index=pd.date_range("2026-08-01", periods=n, freq="5min", tz="UTC"))


@pytest.mark.parametrize("candidate", CANDIDATES)
def test_candidates_have_positive_fixtures_and_prefix_parity(candidate):
    b = bars()
    if candidate == CANDIDATES[0]:
        b.loc[b.index[59], ["open", "low", "close"]] = [99.5, 98., 100.]
        b.loc[b.index[60], ["open", "high", "close"]] = [100., 103., 102.]
    elif candidate == CANDIDATES[1]:
        b.loc[b.index[60], ["open", "low", "high", "close", "volume", "notional"]] = [
            100.1, 100., 101.1, 101., 200., 20100.]
    else:
        b.loc[b.index[60], ["high", "close", "volume"]] = [103., 102., 200.]
    full = scan(b)
    assert full[candidate].iloc[60] == 1
    pd.testing.assert_frame_equal(full.iloc[:65], scan(b.iloc[:65]))
    b.loc[b.index[45], "eligible"] = False
    assert scan(b)[candidate].iloc[60] == 0


def test_vwap_uses_trade_notional_and_session_coverage():
    b = bars(600)
    b["close"] = 110.
    f = scan(b)
    assert f.session_vwap.iloc[60] == 100.
    b.loc[b.index[20], "eligible"] = False
    f = scan(b)
    assert f.session_vwap.iloc[20:288].isna().all()
    assert f.session_vwap.iloc[288] == 100.
    assert scan(b.iloc[30:100]).session_vwap.isna().all()


def test_calendar_and_volume_evidence_required():
    with pytest.raises(ValueError, match="calendar"):
        scan(bars().drop(bars().index[5]))
    with pytest.raises(KeyError):
        scan(bars().drop(columns="notional"))


def replay_fixture(side=1):
    b = bars(20)
    b["atr"] = 2.
    b[CANDIDATES[0]] = 0
    b.loc[b.index[1], CANDIDATES[0]] = side
    return b


@pytest.mark.parametrize("side", [1, -1])
def test_next_open_stop_first_and_fees(side):
    b = replay_fixture(side)
    b.loc[b.index[2], ["high", "low"]] = [110., 90.]
    trades, _ = replay(b, CANDIDATES[0], fee_bps=5.9, slip_bps=0)
    row = trades.iloc[0]
    assert row.reason == "stop"
    assert row.entry_time == b.index[2].isoformat()
    assert row.entry == 100
    assert row.gross_bps == pytest.approx(-300.)
    assert row.net_bps == pytest.approx(-300 - 5.9*(1+row.exit/100))


def test_gap_stop_uses_worse_open_and_slippage_hurts():
    b = replay_fixture()
    b.loc[b.index[3], ["open", "high", "low", "close"]] = [90., 91., 89., 90.]
    plain, _ = replay(b, CANDIDATES[0], fee_bps=0, slip_bps=0)
    cost, _ = replay(b, CANDIDATES[0], fee_bps=5.9, slip_bps=3)
    assert plain.iloc[0].exit == 90.
    assert cost.iloc[0].net_bps < plain.iloc[0].net_bps


def test_missing_future_censors_without_filling_or_overlap():
    b = replay_fixture()
    b.loc[b.index[3], "eligible"] = False
    b.loc[b.index[4], CANDIDATES[0]] = 1
    trades, counts = replay(b, CANDIDATES[0], fee_bps=0, slip_bps=0)
    assert trades.empty
    assert counts["censored"] == 1 and counts["overlap"] == 1
    assert summarize(trades) == {"trades": 0}


def test_timeout_and_entry_extension():
    b = replay_fixture()
    trades, _ = replay(b, CANDIDATES[0], fee_bps=0, slip_bps=0)
    assert trades.iloc[0].reason == "timeout"
    assert trades.iloc[0].exit_bar == b.index[13].isoformat()
    b[CANDIDATES[1]] = b[CANDIDATES[0]]
    b["session_vwap"] = 90.
    trades, counts = replay(b, CANDIDATES[1], fee_bps=0, slip_bps=0)
    assert trades.empty and counts["entry_distance"] == 1


def test_warmup_abstains():
    f = scan(bars())
    assert not f[list(CANDIDATES)].iloc[:49].to_numpy().any()
    assert np.isfinite(f.session_vwap).all()
