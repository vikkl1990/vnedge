"""Signal-funnel deployment contracts."""

from pathlib import Path

import yaml


def compose_services() -> dict:
    return yaml.safe_load(Path("docker-compose.yml").read_text())["services"]


def test_event_leadlag_miner_refreshes_candidate_feed_on_interval():
    services = compose_services()
    service = services["event-leadlag-miner"]

    assert service["command"][:3] == ["python", "-m", "vnedge.research.event_leadlag_alpha"]
    assert "--interval-seconds" in service["command"]
    assert "./data:/app/data:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]


def test_event_leadlag_shadow_can_refresh_public_candle_context():
    services = compose_services()
    service = services["event-leadlag-shadow"]

    assert service["command"][:3] == [
        "python",
        "-m",
        "vnedge.runtime.event_leadlag_shadow_runner",
    ]
    assert "--refresh-bootstrap-minutes" in service["command"]
    assert "./data:/app/data" in service["volumes"]
    assert "./data:/app/data:ro" not in service["volumes"]
