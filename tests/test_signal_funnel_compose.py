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


def test_daily_scalper_and_distillation_refresh_on_slow_interval():
    services = compose_services()
    daily = services["daily-scalper-pack"]
    distill = services["alpha-distillation"]

    assert daily["command"][:3] == ["python", "-m", "vnedge.research.daily_scalper_pack"]
    assert "--interval-seconds" in daily["command"]
    assert "--max-candidates" in daily["command"]
    assert "./data:/app/data:ro" in daily["volumes"]
    assert "./research/live_research:/app/research/live_research" in daily["volumes"]

    assert distill["command"][:3] == ["python", "-m", "vnedge.research.alpha_distillation"]
    assert "--interval-seconds" in distill["command"]
    assert "--max-candidates" in distill["command"]
    assert "./data:/app/data:ro" in distill["volumes"]
    assert "./research/live_research:/app/research/live_research" in distill["volumes"]


def test_orderflow_footprint_miner_refreshes_replay_required_artifact():
    services = compose_services()
    service = services["orderflow-footprint-miner"]

    assert service["command"][:3] == ["python", "-m", "vnedge.research.orderflow_footprint"]
    assert "--interval-seconds" in service["command"]
    assert "--bar-seconds" in service["command"]
    assert "--max-candidates" in service["command"]
    assert "./data:/app/data:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]


def test_alpha_council_waits_for_research_artifact_producers():
    service = compose_services()["alpha-council"]

    assert set(service["depends_on"]) >= {
        "daily-scalper-pack",
        "alpha-distillation",
        "event-leadlag-miner",
        "orderflow-footprint-miner",
        "bitcoin-regime-sensor",
    }


def test_bitcoin_regime_sensor_is_context_only():
    service = compose_services()["bitcoin-regime-sensor"]

    assert service["command"][:3] == ["python", "-m", "vnedge.research.bitcoin_regime"]
    assert "--interval-seconds" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert all("/app/data" not in volume for volume in service["volumes"])
    assert all("/app/logs" not in volume for volume in service["volumes"])
    assert "MEMPOOL_API_BASE" in service["environment"]
