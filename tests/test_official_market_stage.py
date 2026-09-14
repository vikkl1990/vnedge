"""Official context never becomes canonical evidence or backdated live history."""
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.dashboard import analyst_history as h
from vnedge.dashboard import official_market_stage as s
from vnedge.dashboard.analyst_store import AnalystStore
from vnedge.dashboard.analyst_workspace import AnalystWorkspace
from vnedge.dashboard.market_stage import analyse_stage
from vnedge.data.candles import TF_SECONDS

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def source(store, prices=range(200, 300), tf="4h", end=NOW, receipt=None):
    values = list(prices)
    raw = [dict(time=int(end.timestamp())-(len(values)-i)*TF_SECONDS[tf],
                open=p, close=p, high=p+2, low=p-2, volume=10) for i, p in enumerate(values)]
    store.append("official_series", f"BTCUSD/{tf}",
                 dict(source=h.SOURCE, symbol="BTCUSD", timeframe=tf,
                      request_end=int(end.timestamp()), raw=raw), receipt or end)
    return store.read("official_series", f"BTCUSD/{tf}", now=receipt or end)[0]


@pytest.fixture
def store(tmp_path):
    return AnalystStore(tmp_path / "official.sqlite", writable=True, wal=False)


@pytest.mark.parametrize("tf", ["4h", "1d"])
def test_independent_source_memory_and_no_canonical_permission(store, tf):
    rec = source(store, list(range(400, 200, -2))+[200]*150, tf)
    result = s.classify(rec, "BTCUSD", tf, NOW)
    assert result["stage"] == "base_after_decline"
    assert result["prior_direction"] == "down"
    assert result["version"] == "market_stage_official_delta_v1"
    assert result["spec_hash"] == s.SPEC_HASH and result["source"] == h.SOURCE
    assert result["history_kind"] == "reconstructed"
    assert result["collected_at"] == NOW.isoformat()
    assert not result["can_trade"] and not result["can_promote"]
    assert "above their EMA50" in result["watch"][0]["condition"]
    rows = h.normalize(rec["body"]["raw"], "BTCUSD", tf, int(NOW.timestamp()))
    assert analyse_stage(rows, "delta_india", "BTCUSD", tf, NOW)["state"] == "unavailable"


def test_report_identity_idempotency_and_receipt_time_not_backdated(store):
    rec = source(store, receipt=NOW+timedelta(seconds=2))
    assert not s.record_stage(store, "BTCUSD", "4h", NOW)
    assert s.record_stage(store, "BTCUSD", "4h", NOW+timedelta(seconds=3))
    assert not s.record_stage(store, "BTCUSD", "4h", NOW+timedelta(seconds=4))
    assert s.history(store, "BTCUSD", NOW)["stage_reports"] == []
    report = s.history(store, "BTCUSD", NOW+timedelta(seconds=5))["stage_reports"][0]
    assert report["body"]["source_evidence_id"] == rec["evidence_id"]
    assert report["available_at"] == (NOW+timedelta(seconds=3)).isoformat()
    assert report["body"]["event"] == "baseline"
    assert len(s.history(store, "BTCUSD", NOW+timedelta(seconds=5))["stage_reports"]) == 1


def test_latest_source_pending_never_carries_old_conclusion_and_revision_is_visible(store):
    source(store)
    s.record_stage(store, "BTCUSD", "4h", NOW)
    assert s.current_stage(store, "BTCUSD", "4h", NOW)["state"] == "current"
    source(store, prices=range(300, 400), receipt=NOW+timedelta(seconds=1))
    assert s.current_stage(store, "BTCUSD", "4h", NOW+timedelta(seconds=1))["state"] == "unavailable"
    s.record_stage(store, "BTCUSD", "4h", NOW+timedelta(seconds=2))
    reports = s.history(store, "BTCUSD", NOW+timedelta(seconds=2))["stage_reports"]
    assert len(reports) == 2 and reports[0]["body"]["event"] == "source_revision"
    assert reports[0]["body"]["previous_report_id"] == reports[1]["evidence_id"]
    assert s.current_stage(store, "BTCUSD", "4h", NOW+timedelta(hours=7))["state"] == "stale"


@pytest.mark.parametrize("hours,event,prices", [(4,"stage_unchanged",range(201,301)),
                                              (8,"observation_gap",range(202,302)),
                                              (4,"reported_stage_changed",range(400,300,-1))])
def test_forward_reports_distinguish_changes_from_missing_observations(store, hours, event, prices):
    source(store)
    s.record_stage(store, "BTCUSD", "4h", NOW)
    later = NOW+timedelta(hours=hours)
    source(store, prices=prices, end=later)
    s.record_stage(store, "BTCUSD", "4h", later)
    assert s.history(store, "BTCUSD", later)["stage_reports"][0]["body"]["event"] == event


def test_gap_resets_memory_or_rejects_short_suffix(store):
    rec = source(store, list(range(400,200,-2))+[200]*150)
    del rec["body"]["raw"][99]
    result = s.classify(rec, "BTCUSD", "4h", NOW)
    assert result["stage"] == "unknown" and result["prior_direction"] is None
    assert result["issues"] == ["official_stage_history_gap"]
    del rec["body"]["raw"][-20]
    result = s.classify(rec, "BTCUSD", "4h", NOW)
    assert result["state"] == "unavailable" and result["contiguous_bars"] == 19


def test_scope_future_and_invalid_history_refused(store):
    rec = source(store)
    assert s.classify(rec, "BTCUSD", "4h", NOW-timedelta(seconds=1))["state"] == "unavailable"
    rec["body"]["symbol"] = "ETHUSD"
    assert s.classify(rec, "BTCUSD", "4h", NOW)["state"] == "unavailable"
    with pytest.raises(ValueError):
        s.classify(rec, "BTCUSD", "15m", NOW)
    with pytest.raises(ValueError):
        h.OfficialAnalystService(store.path).snapshot("delta_india", "1d")


def test_closed_prefix_causality(store):
    rec = source(store, list(range(200,400))+[400]*120)
    cutoff = NOW-timedelta(hours=(320-151)*4)
    rec["body"]["request_end"] = int(cutoff.timestamp())
    rec["available_at"] = cutoff.isoformat()
    result = s.classify(rec, "BTCUSD", "4h", cutoff)
    rec["body"]["raw"] = rec["body"]["raw"][:151]
    assert result == s.classify(rec, "BTCUSD", "4h", cutoff)
    assert all(e["at"] <= cutoff.isoformat() for e in result["transitions"])


def test_workspace_uses_official_reports_without_relabeling(tmp_path, monkeypatch):
    workspace = AnalystWorkspace(tmp_path / "candles", tmp_path / "analyst/evidence.sqlite")
    writer = AnalystStore(workspace.official.store.path, writable=True, wal=False)
    end = datetime.fromtimestamp(int(datetime.now(UTC).timestamp())//86400*86400, UTC)
    for tf in ("4h", "1d"):
        source(writer, tf=tf, end=end)
        s.record_stage(writer, "BTCUSD", tf, end)
    dossier = workspace.dossier("delta_india", "BTCUSD", "official_delta")
    assert all(stage["source"] == h.SOURCE for stage in dossier["stages"])
    assert all(e["source"] == h.SOURCE for e in dossier["evidence"] if e["kind"] == "market_stage")
    assert len(workspace.history("delta_india", "BTCUSD", "official_delta")["stage_reports"]) == 2
    assert all(stage["state"] == "unavailable" for stage in workspace.dossier("delta_india", "BTCUSD")["stages"])
