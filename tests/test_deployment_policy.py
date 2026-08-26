from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_container_default_and_compose_are_measurement_only() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    final_cmd = next(
        line for line in reversed(dockerfile.splitlines()) if line.startswith("CMD ")
    )
    assert "vnedge.runtime.scanner_startup" in final_cmd
    assert "paper_trial" not in final_cmd

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["multi-lane-shadow"]
    assert service["command"] == ["python", "-m", "vnedge.runtime.scanner_startup"]
    assert "scanner-prereq-backfill" not in compose["services"]
    environment = service["environment"]
    assert environment["MULTI_LANE_CAPITAL_ENABLED"].endswith(":-0}")
    assert environment["MULTI_LANE_CAPITAL_STRATEGY"].endswith(":-}")


def test_sanctioned_deploy_path_runs_fleet_policy_after_recreate() -> None:
    deploy = (ROOT / "scripts" / "deploy.sh").read_text()
    policy_index = deploy.index("python -m vnedge.runtime.fleet_policy")
    recreate_index = deploy.index("recreate_in_waves")
    edge_index = deploy.index("waiting for TLS edge health")
    assert policy_index > recreate_index
    assert edge_index > recreate_index
    assert policy_index > edge_index
    assert '--expected-build-sha "$HEAD_SHA"' in deploy


def test_deploy_restores_canonical_recorder_before_recreating_lanes() -> None:
    deploy = (ROOT / "scripts" / "deploy.sh").read_text()
    start = deploy.index("recreate_in_waves()")
    end = deploy.index("if ! recreate_in_waves", start)
    recreate = deploy[start:end]

    recorder_start = recreate.index(
        "docker compose up -d --no-build pulse-recorder"
    )
    recorder_proof = recreate.index('grep -q "tick recorder:"')
    lane_start = recreate.index(
        "docker compose up -d --no-build multi-lane-shadow"
    )
    assert recorder_start < recorder_proof < lane_start


def test_tls_edge_has_an_explicit_healthcheck() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    healthcheck = compose["services"]["dashboard-tls"]["healthcheck"]
    command = " ".join(healthcheck["test"])
    assert "/healthz" in command
    assert "127.0.0.1:8765" in command


def test_background_recovery_waits_for_scanner_startup_proof() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for service_name in ("gap-recovery", "vision-recovery"):
        dependency = compose["services"][service_name]["depends_on"][
            "multi-lane-shadow"
        ]
        assert dependency["condition"] == "service_healthy"
