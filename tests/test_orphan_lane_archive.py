"""Orphan cleanup is dry-run-first, recoverable, and roster scoped."""

import json
from datetime import UTC, datetime

import pytest

from vnedge.runtime.multi_lane import LaneSpec
from vnedge.runtime.orphan_lane_archive import archive_orphan_lane_artifacts


def _desired() -> list[LaneSpec]:
    return [LaneSpec("active", "binanceusdm", "BTC/USDT:USDT")]


def test_orphan_archive_dry_run_does_not_mutate(tmp_path) -> None:
    (tmp_path / "active.journal.jsonl").write_text("", encoding="utf-8")
    orphan = tmp_path / "old_lane.journal.jsonl"
    orphan.write_text("", encoding="utf-8")
    (tmp_path / "old_lane.account.json").write_text("{}", encoding="utf-8")

    plan = archive_orphan_lane_artifacts(
        tmp_path,
        desired=_desired(),
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert plan.applied is False
    assert plan.lane_ids == ("old_lane",)
    assert plan.files == ("old_lane.account.json", "old_lane.journal.jsonl")
    assert orphan.exists()


def test_orphan_archive_preserves_runtime_wide_portfolio_journal(tmp_path) -> None:
    (tmp_path / "active.journal.jsonl").write_text("", encoding="utf-8")
    portfolio = tmp_path / "shadow_portfolio.journal.jsonl"
    portfolio.write_text("shared state", encoding="utf-8")

    plan = archive_orphan_lane_artifacts(
        tmp_path,
        desired=_desired(),
        apply=True,
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert plan.applied is False
    assert plan.lane_ids == ()
    assert portfolio.read_text(encoding="utf-8") == "shared state"


def test_orphan_archive_moves_all_lane_artifacts_and_writes_manifest(tmp_path) -> None:
    active = tmp_path / "active.journal.jsonl"
    active.write_text("", encoding="utf-8")
    for suffix in ("journal.jsonl", "equity.jsonl", "account.json", "candles.parquet"):
        (tmp_path / f"old_lane.{suffix}").write_text("evidence", encoding="utf-8")
    target = tmp_path / "archive" / "orphans" / "run-1"

    plan = archive_orphan_lane_artifacts(
        tmp_path,
        desired=_desired(),
        apply=True,
        archive_dir=target,
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert plan.applied is True
    assert active.exists()
    assert not (tmp_path / "old_lane.journal.jsonl").exists()
    assert (target / "old_lane.journal.jsonl").read_text(encoding="utf-8") == "evidence"
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["moved_files"] == manifest["planned_files"]


def test_orphan_archive_refuses_external_or_shallow_target(tmp_path) -> None:
    (tmp_path / "old_lane.journal.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="nested below the journal"):
        archive_orphan_lane_artifacts(
            tmp_path,
            desired=_desired(),
            apply=True,
            archive_dir=tmp_path.parent / "outside",
        )
    with pytest.raises(ValueError, match="dedicated nested"):
        archive_orphan_lane_artifacts(
            tmp_path,
            desired=_desired(),
            apply=True,
            archive_dir=tmp_path / "orphans",
        )


def test_partial_archive_failure_leaves_recovery_manifest(tmp_path, monkeypatch) -> None:
    for suffix in ("journal.jsonl", "equity.jsonl"):
        (tmp_path / f"old_lane.{suffix}").write_text("evidence", encoding="utf-8")
    target = tmp_path / "archive" / "orphans" / "failed-run"
    calls = 0

    def fail_second_move(source: str, destination: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated move failure")
        from pathlib import Path

        Path(source).rename(destination)

    monkeypatch.setattr("vnedge.runtime.orphan_lane_archive.shutil.move", fail_second_move)

    with pytest.raises(OSError, match="simulated move failure"):
        archive_orphan_lane_artifacts(
            tmp_path,
            desired=_desired(),
            apply=True,
            archive_dir=target,
        )

    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["moved_files"] == ["old_lane.equity.jsonl"]
