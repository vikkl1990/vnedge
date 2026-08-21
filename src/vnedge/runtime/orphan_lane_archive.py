"""Recoverable cleanup for lane artifacts absent from the desired roster.

Dry-run is the default. ``--apply`` moves every root-level artifact belonging
to an ORPHAN lane into a timestamped archive directory and writes a manifest;
nothing is deleted. Active/desired lane files are never selected.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from vnedge.runtime.lane_health import VERDICT_ORPHAN, audit_lanes
from vnedge.runtime.multi_lane import LaneSpec
from vnedge.runtime.multi_lane_shadow import lane_specs_fingerprint


@dataclass(frozen=True, slots=True)
class OrphanArchivePlan:
    journal_dir: str
    archive_dir: str
    roster_hash: str
    lane_ids: tuple[str, ...]
    files: tuple[str, ...]
    applied: bool

    def to_dict(self) -> dict:
        return asdict(self)


def archive_orphan_lane_artifacts(
    journal_dir: Path | str,
    *,
    desired: list[LaneSpec],
    apply: bool = False,
    archive_dir: Path | str | None = None,
    now: datetime | None = None,
) -> OrphanArchivePlan:
    """Plan or recoverably archive root-level artifacts for orphan lanes."""
    root = Path(journal_dir)
    at = now or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("archive timestamp must be timezone-aware")
    target = Path(archive_dir) if archive_dir is not None else (
        root / "archive" / "orphans" / at.strftime("%Y%m%dT%H%M%SZ")
    )
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
        # Keep recovery evidence under the journal tree. An arbitrary external
        # path is too easy to mistype and makes later restoration ambiguous.
        raise ValueError("archive directory must be nested below the journal directory")
    if target.parent == root:
        # Never put archived lane files directly beside active state where an
        # older or broader artifact scanner could rediscover them.
        raise ValueError("archive directory must use a dedicated nested subdirectory")

    report = audit_lanes(root, desired=desired, now=at.timestamp())
    lane_ids = tuple(
        sorted(row.lane_id for row in report.rows if row.verdict == VERDICT_ORPHAN)
    )
    prefixes = tuple(f"{lane_id}." for lane_id in lane_ids)
    files = tuple(
        sorted(
            path.name
            for path in root.iterdir()
            if path.is_file() and path.name.startswith(prefixes)
        )
    ) if root.is_dir() and prefixes else ()
    roster_hash = lane_specs_fingerprint(desired)

    if apply and files:
        if target.exists():
            raise FileExistsError(f"orphan archive already exists: {target}")
        target.mkdir(parents=True)
        moved: list[str] = []
        manifest_path = target / "manifest.json"
        manifest: dict[str, object] = {
            "archived_at": at.isoformat(),
            "journal_dir": str(root),
            "roster_hash": roster_hash,
            "lane_ids": list(lane_ids),
            "planned_files": list(files),
            "moved_files": moved,
            "recoverable": True,
            "status": "in_progress",
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            for name in files:
                source = root / name
                destination = target / name
                shutil.move(str(source), str(destination))
                moved.append(name)
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["error"] = f"{type(exc).__name__}: {exc}"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            raise
        manifest["status"] = "complete"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return OrphanArchivePlan(
        journal_dir=str(root),
        archive_dir=str(target),
        roster_hash=roster_hash,
        lane_ids=lane_ids,
        files=files,
        applied=bool(apply and files),
    )


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-dir", default="logs/paper_trials")
    parser.add_argument("--archive-dir")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="move orphan artifacts; default is a read-only dry run",
    )
    args = parser.parse_args(argv)
    from vnedge.runtime.multi_lane_shadow import desired_lane_specs

    desired = desired_lane_specs(os.environ if environ is None else environ)
    plan = archive_orphan_lane_artifacts(
        args.journal_dir,
        desired=desired,
        apply=args.apply,
        archive_dir=args.archive_dir,
    )
    print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
