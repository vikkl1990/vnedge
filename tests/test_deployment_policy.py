from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_container_default_and_compose_are_measurement_only() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    final_cmd = next(
        line for line in reversed(dockerfile.splitlines()) if line.startswith("CMD ")
    )
    assert "vnedge.runtime.multi_lane_shadow" in final_cmd
    assert "paper_trial" not in final_cmd

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["multi-lane-shadow"]
    assert service["command"] == ["python", "-m", "vnedge.runtime.multi_lane_shadow"]
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


def test_tls_edge_has_an_explicit_healthcheck() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    healthcheck = compose["services"]["dashboard-tls"]["healthcheck"]
    command = " ".join(healthcheck["test"])
    assert "/healthz" in command
    assert "127.0.0.1:8765" in command
