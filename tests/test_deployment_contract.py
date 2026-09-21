from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_compose_required_variable_is_documented_in_env_example() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    required = set(re.findall(r"\$\{([A-Z0-9_]+):\?[^}]*\}", compose))
    documented = {
        match.group(1)
        for line in example.splitlines()
        if (match := re.match(r"^([A-Z0-9_]+)=", line))
    }
    assert required <= documented


def test_documented_default_start_includes_the_pulse_producer() -> None:
    deploy = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
    assert "multi-lane-shadow pulse-recorder delta-recorder dashboard-tls" in deploy


def test_default_compose_has_no_live_order_service() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "vnedge.runtime.live_trader" not in compose
    assert "vnedge.runtime.paper_trial" not in compose


def test_vm_wrapper_preserves_data_and_uses_guarded_deployer() -> None:
    wrapper = (ROOT / "scripts" / "deploy_vm.sh").read_text()
    assert "docker builder prune --force --filter until=168h" in wrapper
    assert "docker system prune" not in wrapper
    assert "docker volume prune" not in wrapper
    assert "docker compose down" not in wrapper
    assert "VNEDGE_EXPECTED_SHA" in wrapper
    assert 'git show "$expected:scripts/deploy.sh"' in wrapper
    assert "vnedge-rollback:" in wrapper


def test_expected_revision_is_checked_before_merge() -> None:
    script = (ROOT / "scripts" / "deploy.sh").read_text()
    assert script.index("VNEDGE_EXPECTED_SHA") < script.index("git merge --ff-only origin/main")
