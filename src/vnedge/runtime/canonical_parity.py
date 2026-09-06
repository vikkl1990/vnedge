"""Fail-closed evidence for moving the decision clock from Parquet to router.

Dark-mode lanes emit one ``canonical_transport_parity`` record after the
strategy evaluated a candle.  This module folds those records into a bounded
artifact.  The artifact is evidence only: router authority still requires an
explicit mode change and this validator to pass.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

SCHEMA = "vnedge.canonical_transport_parity.v1"


@dataclass(frozen=True, slots=True)
class CanonicalParityPolicy:
    min_duration: timedelta = timedelta(days=7)
    min_observations: int = 100
    min_decisions: int = 1

    def __post_init__(self) -> None:
        if self.min_duration.total_seconds() <= 0:
            raise ValueError("canonical parity duration must be positive")
        if self.min_observations < 1 or self.min_decisions < 1:
            raise ValueError("canonical parity sample minimums must be positive")


def _when(value: object) -> datetime | None:
    try:
        return pd.to_datetime(value, utc=True).to_pydatetime()
    except (TypeError, ValueError):
        return None


def build_canonical_parity_artifact(
    records: Iterable[dict[str, Any]],
    *,
    policy: CanonicalParityPolicy | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    policy = policy or CanonicalParityPolicy()
    observations: list[tuple[datetime, dict[str, Any]]] = []
    for record in records:
        if record.get("kind") != "canonical_transport_parity":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        at = _when(payload.get("evaluated_at") or record.get("ts"))
        if at is not None:
            observations.append((at, payload))
    observations.sort(key=lambda item: item[0])

    mismatches = 0
    identity_gaps = 0
    decisions = 0
    lanes: set[str] = set()
    by_lane: dict[str, list[datetime]] = {}
    for _, payload in observations:
        lane_id = str(payload.get("lane_id") or "unknown")
        lanes.add(lane_id)
        router_hash = str(payload.get("router_bar_hash") or "")
        parquet_hash = str(payload.get("parquet_bar_hash") or "")
        decision_hash = str(payload.get("decision_bar_hash") or "")
        if not router_hash or router_hash != parquet_hash or decision_hash != router_hash:
            mismatches += 1
        decision_ids = [str(item) for item in payload.get("decision_ids") or () if item]
        if decision_ids:
            decisions += len(decision_ids)
        if bool(payload.get("fired")) and not decision_ids:
            identity_gaps += 1
    for at, payload in observations:
        by_lane.setdefault(str(payload.get("lane_id") or "unknown"), []).append(at)

    duration = (
        observations[-1][0] - observations[0][0]
        if len(observations) >= 2
        else timedelta(0)
    )
    blockers: list[str] = []
    if len(observations) < policy.min_observations:
        blockers.append("observations_below_minimum")
    if duration < policy.min_duration:
        blockers.append("duration_below_minimum")
    if decisions < policy.min_decisions:
        blockers.append("decision_identity_sample_missing")
    if mismatches:
        blockers.append("bar_hash_mismatch")
    if identity_gaps:
        blockers.append("fired_without_decision_id")
    lane_windows: list[dict[str, Any]] = []
    for lane_id, stamps in sorted(by_lane.items()):
        lane_duration = stamps[-1] - stamps[0] if len(stamps) >= 2 else timedelta(0)
        lane_blockers: list[str] = []
        if len(stamps) < policy.min_observations:
            lane_blockers.append("observations_below_minimum")
        if lane_duration < policy.min_duration:
            lane_blockers.append("duration_below_minimum")
        if lane_blockers:
            blockers.append(f"lane_window_incomplete:{lane_id}")
        lane_windows.append(
            {
                "lane_id": lane_id,
                "start": stamps[0].isoformat(),
                "end": stamps[-1].isoformat(),
                "duration_seconds": lane_duration.total_seconds(),
                "observations": len(stamps),
                "blockers": lane_blockers,
            }
        )

    now = (generated_at or datetime.now(UTC)).astimezone(UTC)
    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "cutover_ready": not blockers,
        "blockers": blockers,
        "window": {
            "start": observations[0][0].isoformat() if observations else None,
            "end": observations[-1][0].isoformat() if observations else None,
            "duration_seconds": duration.total_seconds(),
        },
        "counts": {
            "observations": len(observations),
            "decisions": decisions,
            "bar_hash_mismatches": mismatches,
            "identity_gaps": identity_gaps,
            "lanes": len(lanes),
        },
        "lanes": lane_windows,
        "policy": {
            "min_duration_seconds": policy.min_duration.total_seconds(),
            "min_observations": policy.min_observations,
            "min_decisions": policy.min_decisions,
        },
    }


def assert_router_authority_artifact(
    path: str | Path,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
) -> dict[str, Any]:
    """Validate a cutover artifact before constructing an authoritative router."""

    artifact_path = Path(path)
    if not artifact_path.is_file():
        raise ValueError(f"router authority evidence missing: {artifact_path}")
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("router authority evidence is unreadable") from exc
    if payload.get("schema") != SCHEMA:
        raise ValueError("router authority evidence schema mismatch")
    if payload.get("cutover_ready") is not True:
        raise ValueError("router authority evidence is not cutover-ready")
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    if float(policy.get("min_duration_seconds") or 0) < timedelta(days=7).total_seconds():
        raise ValueError("router authority evidence policy is shorter than seven days")
    if float(window.get("duration_seconds") or 0) < timedelta(days=7).total_seconds():
        raise ValueError("router authority evidence window is shorter than seven days")
    if int(counts.get("decisions") or 0) < 1:
        raise ValueError("router authority evidence has no decision identity sample")
    if int(counts.get("bar_hash_mismatches") or 0) or int(counts.get("identity_gaps") or 0):
        raise ValueError("router authority evidence contains parity failures")
    generated = _when(payload.get("generated_at"))
    window_end = _when(window.get("end"))
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if generated is None or current - generated > max_age or generated > current + timedelta(minutes=5):
        raise ValueError("router authority evidence is stale or future-dated")
    if window_end is None or current - window_end > max_age or window_end > current + timedelta(minutes=5):
        raise ValueError("router authority evidence window is stale or future-dated")
    return payload


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield journal rows without materialising multi-gigabyte WAL files.

    The parity fold retains only ``canonical_transport_parity`` observations.
    Loading every unrelated lane-eval and quote record first can exceed the
    worker's bounded memory before the filter gets a chance to run.
    """

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-observations", type=int, default=100)
    parser.add_argument("--min-decisions", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    args = parser.parse_args()
    while True:
        paths = [
            nested
            for value in args.journal
            for nested in (
                sorted(Path(value).glob("*.jsonl"))
                if Path(value).is_dir()
                else [Path(value)]
            )
        ]
        records = (row for path in paths for row in _iter_jsonl(path))
        artifact = build_canonical_parity_artifact(
            records,
            policy=CanonicalParityPolicy(
                min_observations=args.min_observations,
                min_decisions=args.min_decisions,
            ),
        )
        atomic_write_json(args.output, artifact)
        print(json.dumps(artifact, sort_keys=True))
        if args.interval_seconds <= 0:
            return
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()


__all__ = [
    "SCHEMA",
    "CanonicalParityPolicy",
    "assert_router_authority_artifact",
    "atomic_write_json",
    "build_canonical_parity_artifact",
]
