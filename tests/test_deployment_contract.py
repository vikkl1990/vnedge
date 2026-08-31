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
