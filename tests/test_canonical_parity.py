from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.runtime.canonical_parity import (
    CanonicalParityPolicy,
    assert_router_authority_artifact,
    atomic_write_json,
    build_canonical_parity_artifact,
)


def _record(at: datetime, *, digest: str = "a" * 64, decisions=()):
    return {
        "ts": at.isoformat(),
        "kind": "canonical_transport_parity",
        "payload": {
            "evaluated_at": at.isoformat(),
            "lane_id": "delta_btc",
            "router_bar_hash": digest,
            "parquet_bar_hash": digest,
            "decision_bar_hash": digest,
            "decision_ids": list(decisions),
            "fired": bool(decisions),
        },
    }


def test_seven_day_exact_window_is_cutover_ready(tmp_path) -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    records = [
        _record(now - timedelta(days=7) + timedelta(hours=i), decisions=("dec_1",) if i == 0 else ())
        for i in range(169)
    ]
    artifact = build_canonical_parity_artifact(
        records,
        policy=CanonicalParityPolicy(min_observations=100),
        generated_at=now,
    )
    path = tmp_path / "parity.json"
    atomic_write_json(path, artifact)

    assert artifact["cutover_ready"] is True
    assert assert_router_authority_artifact(path, now=now)["cutover_ready"] is True


def test_zero_decisions_and_hash_mismatch_never_pass() -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    records = [_record(now - timedelta(days=7)), _record(now)]
    records[-1]["payload"]["parquet_bar_hash"] = "b" * 64

    artifact = build_canonical_parity_artifact(
        records,
        policy=CanonicalParityPolicy(min_observations=1),
        generated_at=now,
    )

    assert artifact["cutover_ready"] is False
    assert "decision_identity_sample_missing" in artifact["blockers"]
    assert "bar_hash_mismatch" in artifact["blockers"]


def test_stale_authority_artifact_is_rejected(tmp_path) -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    artifact = {
        "schema": "vnedge.canonical_transport_parity.v1",
        "generated_at": (now - timedelta(days=2)).isoformat(),
        "cutover_ready": True,
        "policy": {"min_duration_seconds": 7 * 86400},
        "window": {"duration_seconds": 7 * 86400},
        "counts": {"decisions": 1, "bar_hash_mismatches": 0, "identity_gaps": 0},
    }
    path = tmp_path / "stale.json"
    atomic_write_json(path, artifact)

    with pytest.raises(ValueError, match="stale"):
        assert_router_authority_artifact(path, now=now)


def test_short_lane_windows_cannot_be_combined_into_a_seven_day_pass() -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    first = _record(now - timedelta(days=7), decisions=("dec_1",))
    second = _record(now)
    second["payload"]["lane_id"] = "delta_eth"

    artifact = build_canonical_parity_artifact(
        [first, second],
        policy=CanonicalParityPolicy(min_observations=1),
        generated_at=now,
    )

    assert artifact["cutover_ready"] is False
    assert any(item.startswith("lane_window_incomplete:") for item in artifact["blockers"])
