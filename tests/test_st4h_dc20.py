import pandas as pd
import pytest

from vnedge.research.st4h_dc20 import features, replay, signal


def bars(n=25, freq="1D"):
    return pd.DataFrame({"open": 1000., "high": 1100., "low": 900., "close": 1000.,
                             "volume": 1., "complete": True},
                        index=pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC"))


def test_dc_prior_only_equality_and_no_adr():
    b = bars()
    b.loc[b.index[20], ["high", "close"]] = [1200, 1100]
    f = features(b, "DC20")
    assert f.iloc[20].hh == 1100
    assert signal(f.iloc[20], "DC20", b.index[21])["action"] == "WAIT"
    b.loc[b.index[20], "close"] = 1150
    s = signal(features(b, "DC20").iloc[20], "DC20", b.index[21])
    assert (s["action"], s["stop"], s["target"]) == ("SIGNAL", 900, 1350)


def test_entry_extremes_no_kill_stop_first_no_reentry():
    b = bars()
    b.loc[b.index[20], ["high", "low", "close"]] = [1600, 800, 1150]
    b.loc[b.index[21], ["high", "low", "close"]] = [1200, 950, 1000]
    b.loc[b.index[22], ["high", "low", "close"]] = [1800, 800, 1700]
    r = replay(b, "DC20", b.index[20], b.index[-1]+pd.Timedelta(days=1))
    assert [e["action"] for e in r["events"][:3]] == ["SIGNAL", "MANAGE", "EXIT"]
    assert r["trades"][0]["reason"] == "stop_and_target"
    assert r["trades"][0]["exit"] == 900


def test_close_hour_filter_not_open_hour():
    row = pd.Series({"complete": True, "band": 900., "close": 1000., "trend": 1, "flip": True})
    assert signal(row, "ST4H", pd.Timestamp("2026-01-01T08:00Z"))["action"] == "SKIP_ASIA"
    assert signal(row, "ST4H", pd.Timestamp("2026-01-01T12:00Z"))["action"] == "SIGNAL"
    row["complete"] = False
    assert signal(row, "ST4H", pd.Timestamp("2026-01-01T12:00Z"))["action"] == "WATCH"


def test_atr_seed_variants_causality_and_cooldown():
    b = bars(40, "4h")
    b.loc[b.index[15], ["high", "low", "close"]] = [2000, 100, 150]
    b.loc[b.index[16], ["high", "low", "close"]] = [2000, 100, 150]
    w, s = features(b, "ST4H"), features(b, "ST4H", "sma")
    assert w.iloc[13].atr == 200
    assert not w.iloc[15].flip  # only two bars since initial seed
    assert w.iloc[16].flip
    assert w.iloc[16].atr != s.iloc[16].atr
    pd.testing.assert_frame_equal(w.iloc[:20], features(b.iloc[:20], "ST4H"))


def test_missing_bar_latches_position_and_resets_warmup():
    b = bars()
    b.loc[b.index[20], ["high", "close"]] = [1200, 1150]
    b = b.drop(b.index[21])
    r = replay(b, "DC20", b.index[20], b.index[-1]+pd.Timedelta(days=1))
    assert r["unresolved"] and r["open_position"]
    assert not r["trades"]
    st = bars(40, "4h")
    st.loc[st.index[20], "complete"] = False
    f = features(st, "ST4H")
    assert pd.isna(f.iloc[33].band)
    assert pd.notna(f.iloc[34].band)


def test_repeat_runs_are_isolated_and_inventory_not_closed():
    b = bars(21)
    b.loc[b.index[20], ["high", "close"]] = [1200, 1150]
    args = (b, "DC20", b.index[20], b.index[-1]+pd.Timedelta(days=1))
    a, c = replay(*args), replay(*args)
    assert a == c and a["open_position"] and a["metrics"]["n"] == 0


def test_bad_variant_refused():
    with pytest.raises(ValueError):
        features(bars(), "OTHER")


def test_asia_does_not_block_exit(monkeypatch):
    b = bars(5, "4h")
    b["band"], b["trend"], b["flip"] = 900., 1, True
    b["atr"] = 40.
    # Entry at 20:00 close, exit at 00:00 close (Asia).
    b.index = pd.date_range("2026-01-01T16:00Z", periods=5, freq="4h")
    monkeypatch.setattr("vnedge.research.st4h_dc20.features", lambda *args: b)
    r = replay(b, "ST4H", b.index[0], b.index[-1]+pd.Timedelta(hours=4))
    assert r["events"][0]["action"] == "SIGNAL"
    assert r["events"][1]["action"] == "EXIT"
    assert r["trades"][0]["reason"] == "stop_and_target"
