from datetime import UTC, datetime, timedelta

import pytest

from vnedge.dashboard import analyst_store as module


def test_full_store_rolls_without_deleting_history(tmp_path, monkeypatch):
    path = tmp_path / "evidence.sqlite"
    store = module.AnalystStore(path, writable=True)
    now = datetime.now(UTC)
    first = store.append("collector", "delta", {"value": 1}, now)
    monkeypatch.setattr(module, "MAX_DATABASE_BYTES", 1)
    second = store.append("collector", "delta", {"value": 2}, now + timedelta(seconds=1))
    assert path.exists()
    assert len(store._segments()) == 2
    readonly = module.AnalystStore(path)
    assert [r["evidence_id"] for r in readonly.read("collector", "delta", now=now + timedelta(seconds=2), limit=10)] == [second, first]
    assert readonly.read("collector", "delta", now=now)[0]["evidence_id"] == first


def test_segment_limit_fails_closed(tmp_path, monkeypatch):
    store = module.AnalystStore(tmp_path / "evidence.sqlite", writable=True)
    monkeypatch.setattr(module, "MAX_DATABASE_BYTES", 1)
    monkeypatch.setattr(module, "MAX_SEGMENTS", 1)
    with pytest.raises(ValueError, match="archive_required"):
        store.append("collector", "delta", {}, datetime.now(UTC))


def test_segment_symlink_refused(tmp_path):
    store = module.AnalystStore(tmp_path / "evidence.sqlite", writable=True)
    (tmp_path / "evidence.sqlite.segment-000001.sqlite").symlink_to(store.path)
    with pytest.raises(ValueError, match="invalid_evidence_segments"):
        store.read("collector", "delta", now=datetime.now(UTC))
