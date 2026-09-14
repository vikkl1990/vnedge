"""Regression coverage for issues reproduced by the read-only UI audit."""
from pathlib import Path
from datetime import UTC, datetime, timedelta
from fastapi.testclient import TestClient

from vnedge.dashboard.trade_journal import build_trade_journal
from vnedge.dashboard.analyst_workspace import dossier_cache_current
from vnedge.dashboard.app import SnapshotProvider, create_app


def test_dossier_cache_cannot_extend_closed_bar_or_fundamental_expiry() -> None:
    now = datetime(2026, 9, 14, tzinfo=UTC)
    body = {"frames": [], "stages": [], "fundamentals": {"fields": []}}
    assert dossier_cache_current(body, now)
    for key, tf, seconds in (("frames", "5m", 450), ("stages", "1d", 129600)):
        body[key] = [{"state": "current", "timeframe": tf, "as_of": (now - timedelta(seconds=seconds)).isoformat()}]
        assert dossier_cache_current(body, now)
        assert not dossier_cache_current(body, now + timedelta(seconds=1))
        body[key] = []
    body["fundamentals"]["fields"] = [{"status": "current", "period_end": (now - timedelta(days=3)).isoformat(), "received_at": now.isoformat(), "max_age_seconds": 259200}]
    assert dossier_cache_current(body, now)
    assert not dossier_cache_current(body, now + timedelta(seconds=1))


def test_missing_sources_are_not_an_empty_reconciled_book(tmp_path: Path) -> None:
    result = build_trade_journal(snapshot={}, journal_dir=tmp_path)
    assert result["source_coverage"]["state"] == "partial"
    assert "no_journal_or_fill_sources" in result["source_coverage"]["issues"]
    assert result["source_coverage"]["history_complete"] is False
    assert result["can_trade"] is False
    assert "active_fleet_scope_unavailable" in result["source_coverage"]["issues"]


def test_journal_api_exposes_source_gaps_and_remains_read_only(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(SnapshotProvider(), token="audit-test"))
    headers = {"Authorization": "Bearer audit-test"}
    assert client.get("/trade-journal").status_code == 401
    response = client.get("/trade-journal", headers=headers)
    assert response.status_code == 200
    assert response.json()["source_coverage"]["state"] == "partial"
    assert response.json()["can_trade"] is False
    assert response.json()["can_promote"] is False
    assert client.post("/trade-journal", headers=headers).status_code == 405


def test_corrupt_source_is_visible_even_when_other_records_are_readable(tmp_path: Path) -> None:
    (tmp_path / "btc.journal.jsonl").write_text('{"kind":"lane_eval","payload":{}}\nnot-json\n[]\n')
    result = build_trade_journal(snapshot={"lane_id": "btc"}, journal_dir=tmp_path)
    assert result["source_coverage"]["state"] == "partial"
    assert "invalid_source_record" in result["source_coverage"]["issues"]


def test_valid_empty_journal_is_distinct_from_missing_active_lane(tmp_path: Path) -> None:
    (tmp_path / "btc.journal.jsonl").write_text("")
    result = build_trade_journal(snapshot={"lane_id": "btc"}, journal_dir=tmp_path)
    assert result["source_coverage"]["state"] == "bounded_window"
    assert result["summary"]["actual_closed_net_usd"] == 0
    result = build_trade_journal(snapshot={"lanes": [{"lane_id": "btc"}, {"lane_id": "eth"}]}, journal_dir=tmp_path)
    assert "active_lane_journals_missing" in result["source_coverage"]["issues"]
