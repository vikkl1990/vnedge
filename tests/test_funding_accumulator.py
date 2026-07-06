"""Durable live-funding accumulation for history-less venues (Delta)."""


import pandas as pd

from vnedge.runtime.funding_accumulator import (
    LivePersistentFundingMR,
    append_funding_sample,
    load_funding_store,
)


def _ts(ms):
    return pd.to_datetime(ms, unit="ms", utc=True)


class _FakeFeed:
    exchange_id = "delta_india"

    def __init__(self, funding_rate=0.0):
        self.funding_rate = funding_rate


def _candles(n, start_ms=1_700_000_000_000, step_ms=3_600_000, price=100.0):
    ts = [start_ms + i * step_ms for i in range(n)]
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(ts, unit="ms", utc=True),
            "open": price, "high": price + 1, "low": price - 1,
            "close": price, "volume": 1.0,
        }
    )


def test_store_roundtrip_dedupes_and_sorts(tmp_path):
    p = tmp_path / "f.jsonl"
    append_funding_sample(p, _ts(2000), 0.02)
    append_funding_sample(p, _ts(1000), 0.01)
    append_funding_sample(p, _ts(2000), 0.02)  # duplicate ts
    df = load_funding_store(p)
    assert list(df["timestamp"]) == [_ts(1000), _ts(2000)]  # sorted, deduped
    assert list(df["funding_rate"]) == [0.01, 0.02]


def test_load_missing_file_is_empty(tmp_path):
    df = load_funding_store(tmp_path / "nope.jsonl")
    assert df.empty
    assert list(df.columns) == ["timestamp", "funding_rate"]


def test_load_skips_malformed_lines(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"ts_ms":1000,"funding_rate":0.01}\ngarbage\n{"bad":true}\n')
    df = load_funding_store(p)
    assert len(df) == 1
    assert df.loc[0, "funding_rate"] == 0.01


def test_cold_start_builds_and_persists_live(tmp_path):
    # empty seed (Delta) -> synthetic warmup anchor, no crash, then accumulate
    store = tmp_path / "delta_btc.funding.jsonl"
    feed = _FakeFeed(funding_rate=0.001)
    strat = LivePersistentFundingMR(
        pd.DataFrame(columns=["timestamp", "funding_rate"]),
        feed,
        store_path=store,
        funding_pct_window=5, z_window=3,
    )
    # first prepare with a candle newer than the 1970 anchor appends one sample
    strat.prepare(_candles(10))
    feed.funding_rate = 0.002
    strat.prepare(_candles(11))  # one more (newest bar advanced)

    persisted = load_funding_store(store)
    # two live samples persisted (the 1970 synthetic anchor is never written)
    assert len(persisted) == 2
    assert min(persisted["timestamp"]) > _ts(0)
    assert persisted["funding_rate"].tolist() == [0.001, 0.002]


def test_restart_resumes_window_from_store(tmp_path):
    store = tmp_path / "delta_btc.funding.jsonl"
    # simulate a prior run having accumulated 3 samples
    for i, fr in enumerate([0.001, 0.002, 0.003], start=1):
        append_funding_sample(store, _ts(1_700_000_000_000 + i * 3_600_000), fr)

    feed = _FakeFeed(funding_rate=0.004)
    strat = LivePersistentFundingMR(
        pd.DataFrame(columns=["timestamp", "funding_rate"]),
        feed, store_path=store, funding_pct_window=5, z_window=3,
    )
    # loaded the 3 prior samples; no synthetic anchor needed
    assert len(strat.funding) == 3
    assert strat.funding["funding_rate"].tolist() == [0.001, 0.002, 0.003]

    # a new observation appends without re-persisting the loaded 3
    strat.prepare(_candles(20))
    reloaded = load_funding_store(store)
    assert len(reloaded) == 4  # 3 prior + 1 new, no duplication


def test_warmup_produces_no_signal_until_window_fills(tmp_path):
    # a real signal needs a full percentile window; a cold lane must stay silent
    feed = _FakeFeed(funding_rate=0.05)
    strat = LivePersistentFundingMR(
        pd.DataFrame(columns=["timestamp", "funding_rate"]),
        feed, store_path=tmp_path / "f.jsonl",
        funding_pct_window=240, z_window=48,
    )
    df = strat.prepare(_candles(60))  # far fewer bars than the 240 window
    # funding_pct is NaN across the board -> signal() must return None
    assert df["funding_pct"].isna().all()
    assert all(strat.signal(df, i) is None for i in range(len(df)))


def test_memory_is_trimmed_but_disk_stays_complete(tmp_path, monkeypatch):
    store = tmp_path / "f.jsonl"
    feed = _FakeFeed(funding_rate=0.001)
    strat = LivePersistentFundingMR(
        pd.DataFrame(columns=["timestamp", "funding_rate"]),
        feed, store_path=store, funding_pct_window=5, z_window=3,
    )
    monkeypatch.setattr(strat, "_MEMORY_CAP", 10)
    # advance the newest bar 40 times -> 40 live samples
    for i in range(40):
        feed.funding_rate = 0.001 + i * 1e-5
        strat.prepare(_candles(10 + i))
    assert len(strat.funding) <= 10               # in-memory bounded
    assert len(load_funding_store(store)) == 40    # disk complete


def test_full_candle_window_with_sparse_real_samples_stays_silent(tmp_path):
    # REGRESSION (VM 2026-07-06): 450+ seeded bars filled the 240-bar
    # percentile window with anchor-propagated zeros, ranking the first real
    # prints as "extreme" -> bogus shadow short. With real samples covering
    # only the last few bars, funding_pct must stay NaN — everywhere.
    feed = _FakeFeed(funding_rate=0.0001)
    strat = LivePersistentFundingMR(
        pd.DataFrame(columns=["timestamp", "funding_rate"]),
        feed, store_path=tmp_path / "f.jsonl",
        funding_pct_window=240, z_window=48,
    )
    df = strat.prepare(_candles(500))  # window CAN fill on bar count alone
    assert df["funding_pct"].isna().all()
    assert all(strat.signal(df, i) is None for i in range(len(df)))


def test_mask_lifts_once_real_samples_span_the_window(tmp_path):
    # store holds 300 hourly samples -> real coverage spans the 240 window;
    # recent bars must get a REAL percentile again (not stay masked forever)
    store = tmp_path / "f.jsonl"
    start = 1_700_000_000_000
    for i in range(300):
        append_funding_sample(store, _ts(start + i * 3_600_000), 0.0001 + i * 1e-7)
    feed = _FakeFeed(funding_rate=0.001)
    strat = LivePersistentFundingMR(
        pd.DataFrame(columns=["timestamp", "funding_rate"]),
        feed, store_path=store, funding_pct_window=240, z_window=48,
    )
    df = strat.prepare(_candles(300, start_ms=start))
    assert df["funding_pct"].iloc[-1] == df["funding_pct"].iloc[-1]  # not NaN
