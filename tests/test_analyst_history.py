from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from vnedge.dashboard import analyst_history as h
from vnedge.dashboard.analyst_store import AnalystStore
from vnedge.dashboard.analyst_workspace import AnalystWorkspace
from vnedge.dashboard.crypto_analyst import analyse_rows

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
END = int(NOW.timestamp())


def raw(count=100):
    return [dict(time=END - (count-i)*900, open=100+i, high=103+i, low=99+i,
                 close=102+i, volume=10+i, quote_volume=999999) for i in range(count)]


def save(path, values=None, **changes):
    store = AnalystStore(path, writable=True)
    body = dict(source=h.SOURCE, symbol="BTCUSD", timeframe="15m", request_end=END, raw=raw() if values is None else values)
    body.update(changes)
    store.append("official_series", "BTCUSD/15m", body, NOW)
    return store


def test_official_analysis_cannot_be_canonical_or_exact_vwap(tmp_path):
    store = save(tmp_path / "evidence.sqlite")
    result = h.frame(store, "BTCUSD", "15m", NOW)
    assert result["state"] == "current"
    assert result["source"] == h.SOURCE
    assert result["metrics"]["session_vwap"] is None
    assert not result["can_trade"] and not result["can_promote"]
    assert len(result["sparkline"]) == 40
    rows = h.normalize(raw(), "BTCUSD", "15m", END)
    assert "quote_volume" not in rows[0] and "coverage_ok" not in rows[0]
    assert analyse_rows(rows, "BTCUSD", "delta_india", "15m", NOW)["state"] == "unavailable"


@pytest.mark.parametrize("change", [{"time": END-899}, {"time": True}, {"close": float("nan")}, {"low": -1}, {"high": 1}, {"volume": None}])
def test_malformed_data_rejected(change):
    values = raw()
    values[-1].update(change)
    with pytest.raises(ValueError):
        h.normalize(values, "BTCUSD", "15m", END)


def test_duplicates_and_forming():
    values = raw()
    assert len(h.normalize(values + [values[-1], {**values[-1], "time": END}], "BTCUSD", "15m", END)) == 100
    with pytest.raises(ValueError, match="conflicting"):
        h.normalize(values + [{**values[-1], "volume": 999}], "BTCUSD", "15m", END)


def test_gap_is_not_padded_and_stale_is_not_current(tmp_path):
    values = raw()
    del values[-20]
    store = save(tmp_path / "gap.sqlite", values)
    result = h.frame(store, "BTCUSD", "15m", NOW)
    assert result["state"] == "unavailable"
    assert result["history"]["contiguous_bars"] == 19
    store = save(tmp_path / "stale.sqlite")
    assert h.frame(store, "BTCUSD", "15m", NOW+timedelta(hours=1))["state"] == "stale"


def test_future_availability_scope_and_readonly(tmp_path):
    path = tmp_path / "test.sqlite"
    store = save(path)
    assert h.frame(store, "BTCUSD", "15m", NOW-timedelta(seconds=1))["state"] == "unavailable"
    with pytest.raises(ValueError):
        AnalystStore(path).append("test", "test", {}, NOW)
    with pytest.raises(ValueError):
        h.validate_scope("bybit", "BTCUSD", "15m")
    save(path, source="canonical_tick_lake")
    assert h.frame(store, "BTCUSD", "15m", NOW)["state"] == "unavailable"


def test_collector_idempotent_and_requires_latest(monkeypatch, tmp_path):
    store = AnalystStore(tmp_path / "test.sqlite", writable=True)
    monkeypatch.setattr(h, "fetch", lambda *args: raw())
    # Persist availability uses wall time; use an as-of after that wall time.
    now = datetime.now(UTC) + timedelta(seconds=10)
    end = int(now.timestamp()-5)//900*900
    values = [{**r, "time": r["time"] + end-END} for r in raw()]
    monkeypatch.setattr(h, "fetch", lambda *args: values)
    assert h.collect_cell(store, "BTCUSD", "15m", now)
    assert not h.collect_cell(store, "BTCUSD", "15m", now)
    monkeypatch.setattr(h, "fetch", lambda *args: [])
    with pytest.raises(ValueError, match="latest"):
        h.collect_cell(store, "BTCUSD", "15m", now+timedelta(minutes=15))


def test_workspace_sources_do_not_fallback(tmp_path):
    workspace = AnalystWorkspace(tmp_path / "candles", tmp_path / "analyst/evidence.sqlite")
    assert workspace.snapshot("delta_india", "15m")["selected_source"] == "canonical"
    report = workspace.snapshot("delta_india", "15m", "official_delta")
    assert len(report["markets"]) == 12
    assert report["universe"]["current"] == 0
    assert report["source"] == h.SOURCE
    with pytest.raises(ValueError):
        workspace.snapshot("bybit", "15m", "official_delta")
    dossier = workspace.dossier("delta_india", "BTCUSD", "official_delta")
    assert dossier["selected_source"] == "official_delta"
    assert all(s["state"] == "unavailable" for s in dossier["stages"])


def test_history_rollback_journal_needs_no_writable_reader_sidecars(tmp_path):
    path = tmp_path / "history.sqlite"
    store = AnalystStore(path, writable=True, wal=False)
    store.append("collector", "delta_india", {"ok": True}, NOW)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert AnalystStore(path).read("collector", "delta_india", now=NOW)[0]["body"] == {"ok": True}
    assert not list(tmp_path.glob("*-shm"))
    assert not list(tmp_path.glob("*-wal"))
