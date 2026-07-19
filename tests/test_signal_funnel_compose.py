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


def test_dashboard_reads_pine_research_kb_from_host_artifact():
    service = compose_services()["multi-lane-shadow"]

    assert "./research/pine_scripts:/app/research/pine_scripts:ro" in service["volumes"]


def test_pine_backtest_evidence_refreshes_matrix_overlay():
    service = compose_services()["pine-backtest-evidence"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == ["python", "-m", "vnedge.research.pine_backtest_evidence"]
    assert "--interval-seconds" in service["command"]
    assert "--report-dir" in service["command"]
    assert "./research/pine_scripts:/app/research/pine_scripts" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) >= {
        "daily-scalper-pack",
        "daily-scalper-cadence",
        "alpha-distillation",
        "orderflow-footprint-miner",
        "event-leadlag-miner",
        "candidate-replay-executor",
        "pine-alpha-distiller",
    }


def test_pine_alpha_distiller_refreshes_source_intention_artifact():
    service = compose_services()["pine-alpha-distiller"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == ["python", "-m", "vnedge.research.pine_alpha_distiller"]
    assert "--interval-seconds" in service["command"]
    assert "--source-dir" in service["command"]
    assert "research/pine_scripts/sources" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/pine_alpha_distiller_latest.json" in service["command"]
    assert "./research/pine_scripts:/app/research/pine_scripts:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]


def test_pine_edge_uplift_agent_recycles_failed_evidence_only():
    service = compose_services()["pine-edge-uplift-agent"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == ["python", "-m", "vnedge.research.pine_edge_uplift_agent"]
    assert "--interval-seconds" in service["command"]
    assert "--distiller" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/pine_edge_uplift_agent_latest.json" in service["command"]
    assert "./research/pine_scripts:/app/research/pine_scripts:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert service["depends_on"] == ["pine-backtest-evidence"]


def test_edge_uplift_executor_materializes_agent_tasks():
    service = compose_services()["edge-uplift-executor"]

    assert service["user"] == "${VNEDGE_CONTAINER_UID:-1000}:${VNEDGE_CONTAINER_GID:-1000}"
    assert service["command"][:3] == ["python", "-m", "vnedge.research.edge_uplift_executor"]
    assert "--interval-seconds" in service["command"]
    assert "--uplift" in service["command"]
    assert "research/live_research/pine_edge_uplift_agent_latest.json" in service["command"]
    assert "--scanner" in service["command"]
    assert "research/live_research/scanner_tournament_latest.json" in service["command"]
    assert "--out" in service["command"]
    assert "research/live_research/edge_uplift_experiments_latest.json" in service["command"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert set(service["depends_on"]) == {"pine-edge-uplift-agent", "scanner-tournament"}


def test_scanner_tournament_lowers_only_research_discovery_governance():
    service = compose_services()["scanner-tournament"]

    assert service["command"][:3] == ["python", "-m", "vnedge.research.scanner_tournament"]
    assert "--profile" in service["command"]
    assert "${SCANNER_TOURNAMENT_PROFILE:-discovery_relaxed}" in service["command"]
    assert "--timeframes" in service["command"]
    assert "${SCANNER_TOURNAMENT_TIMEFRAMES:-1m,5m,15m,1h,4h}" in service["command"]
    assert "--progress" in service["command"]
    assert "research/live_research/scanner_tournament_progress.json" in service["command"]
    assert "./data:/app/data:ro" in service["volumes"]
    assert "./research/live_research:/app/research/live_research" in service["volumes"]
    assert "RESEARCH_EXCHANGES" in service["environment"]


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
        "scanner-tournament",
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
