import numpy as np
import pandas as pd
import pytest

from vnedge.research.dc20_vol import compare
from vnedge.research.st4h_dc20 import features, replay, signal, vol_ok


def bars(n=70):
    return pd.DataFrame({"open": 1000., "high": 1100., "low": 900., "close": 1000.,
                         "volume": 1., "complete": True},
                        index=pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC"))


@pytest.mark.parametrize("mode,value,expected", [
    ("atr_exp", 1., True), ("atr_exp", .999, False), ("atr_mid", .85, True),
    ("atr_mid", 1.30, True), ("atr_mid", 1.301, False), ("adr_band", .75, True),
    ("adr_band", 1.40, True), ("adr_band", 1.401, False),
    ("skip_comp", .75, True), ("skip_comp", .749, False), ("atr_exp", np.nan, False)])
def test_gate_boundaries(mode, value, expected):
    assert vol_ok(pd.Series({"atr_ratio": value, "adr_ratio": value}), mode) is expected


def test_sma_tr_differs_from_adr_and_is_causal():
    b = bars()
    b.loc[b.index[60], ["open", "high", "low", "close"]] = [1500, 1600, 1400, 1500]
    f = features(b, "DC20")
    assert f.iloc[60].atr14 == pytest.approx((13*200+600)/14)
    assert f.iloc[60].adr14 == 200
    pd.testing.assert_frame_equal(f.iloc[:61], features(b.iloc[:61], "DC20"))
    b.loc[b.index[61], "complete"] = False
    assert pd.isna(features(b, "DC20").iloc[62].atr_ratio)


def test_off_does_not_require_vol_warmup_and_watch_does_not_fire():
    b = bars(21)
    b.loc[b.index[-1], ["high", "close"]] = [1200, 1150]
    row = features(b, "DC20").iloc[-1]
    assert signal(row, "DC20", b.index[-1])["action"] == "SIGNAL"
    assert signal(row, "DC20", b.index[-1], "atr_exp")["action"] == "SKIP_VOL"
    row["complete"] = False
    assert signal(row, "DC20", b.index[-1], "atr_exp")["action"] == "WATCH"
    zero = bars()
    zero[["open", "high", "low", "close"]] = 1000.
    assert not vol_ok(features(zero, "DC20").iloc[-1], "atr_exp")


def test_gate_not_applied_to_management_and_occupancy_is_replayed(monkeypatch):
    b = bars()
    b.loc[b.index[20], ["high", "close"]] = [1200, 1150]
    b.loc[b.index[21]:b.index[54], ["high", "low", "close"]] = [1200, 1100, 1150]
    b.loc[b.index[55], ["high", "low", "close"]] = [2000, 500, 1300]
    start, end = b.index[20], b.index[-1]+pd.Timedelta(days=1)
    off = replay(b, "DC20", start, end)
    prepared = features(b, "DC20")
    prepared.loc[b.index[56], "atr_ratio"] = .5
    assert not vol_ok(prepared.iloc[56], "atr_exp")
    monkeypatch.setattr("vnedge.research.st4h_dc20.features", lambda *args: prepared)
    gate = replay(b, "DC20", start, end, vol_mode="atr_exp")
    diff = compare(off, gate)
    assert diff["added_entry_ids"] and diff["removed_entry_ids"]
    assert gate["trades"]  # later stop still exits even though expansion no longer passes
    assert gate["trades"][0]["reason"] == "stop"


def test_unknown_mode_and_st4h_gate_refused():
    with pytest.raises(ValueError):
        vol_ok(pd.Series(dtype=float), "widest")
    b = bars()
    with pytest.raises(ValueError):
        replay(b, "ST4H", b.index[0], b.index[-1], vol_mode="atr_exp")
