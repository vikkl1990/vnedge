"""Dashboard — auth gates, snapshot schema, read-only surface."""

import json

import pytest
from fastapi.testclient import TestClient

from vnedge.config.risk_config import RiskConfig
from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.state_snapshot import FeedHealth, build_snapshot
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.portfolio_tracker import PortfolioTracker

SYM = "BTC/USDT:USDT"


@pytest.fixture
def client():
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    return TestClient(create_app(provider, token="t3st-token"))


def test_empty_token_refused_at_construction():
    with pytest.raises(ValueError, match="no token, no dashboard"):
        create_app(SnapshotProvider(), token="")


def test_state_requires_token(client):
    assert client.get("/state").status_code == 401
    assert client.get("/state?token=wrong").status_code == 401


def test_state_with_token(client):
    r = client.get("/state", headers={"Authorization": "Bearer t3st-token"})
    assert r.status_code == 200
    assert r.json()["mode"] == "shadow"
    assert client.get("/state?token=t3st-token").status_code == 200


def test_dashboard_shell_contains_quant_cockpit_panels(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "VNEDGE Operator Cockpit" in html
    assert "institutional scalper and research cockpit" in html
    assert "Portfolio Command" in html
    assert "Sharpe" in html
    assert "Profit Factor" in html
    assert "Data Lanes" in html
    assert "Signal Funnel" in html
    assert "evaluated -> fired -> approved -> submitted -> filled" in html
    assert "Active Lane Matrix" in html
    assert "Execution Truth" in html
    assert "Research Lanes" in html
    assert "Agent Council" in html
    assert "Governance Rail" in html
    assert "Trade Journal" in html
    assert "Research Proof Queue" in html
    assert "Model And Edge Health" in html
    assert "TAKER IF EDGE CLEARS" in html


def test_no_snapshot_yet_is_503():
    app = create_app(SnapshotProvider(), token="t3st-token")
    r = TestClient(app).get("/state?token=t3st-token")
    assert r.status_code == 503


def test_websocket_requires_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_json()


def test_websocket_pushes_snapshot(client):
    with client.websocket_connect("/ws?token=t3st-token") as ws:
        assert ws.receive_json()["equity"] == 500.0


def test_history_endpoint_auth_and_content(tmp_path):
    import json

    hist = tmp_path / "eq.jsonl"
    hist.write_text(
        "\n".join(json.dumps({"ts": f"2026-07-03T0{i}:00:00+00:00", "equity": 500.0 + i})
                  for i in range(3))
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "paper"})
    client = TestClient(create_app(provider, token="t3st-token", history_path=hist))
    assert client.get("/history").status_code == 401
    points = client.get("/history?token=t3st-token").json()
    assert len(points) == 3
    assert points[-1]["equity"] == 502.0


def test_history_without_file_is_empty(client):
    assert client.get("/history?token=t3st-token").json() == []


def test_alpha_council_and_workbench_endpoints_are_auth_gated(tmp_path):
    council = tmp_path / "alpha_council_latest.json"
    workbench = tmp_path / "alpha_workbench_latest.json"
    council.write_text(json.dumps({
        "summary": {"debated": 2},
        "debates": [{"next_action": "RUN_CONSERVATIVE_L2_REPLAY"}],
        "can_trade": False,
        "can_promote": False,
    }))
    workbench.write_text(json.dumps({
        "summary": {"open_tasks": 1},
        "tasks": [{"task_type": "conservative_replay"}],
        "can_trade": False,
        "can_promote": False,
    }))
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(create_app(
        provider,
        token="t3st-token",
        alpha_council_path=council,
        alpha_workbench_path=workbench,
    ))

    assert client.get("/alpha-council").status_code == 401
    assert client.get("/alpha-workbench").status_code == 401
    assert client.get("/alpha-council?token=t3st-token").json()["summary"]["debated"] == 2
    assert client.get("/alpha-workbench?token=t3st-token").json()["summary"]["open_tasks"] == 1


def test_alpha_council_and_workbench_missing_files_are_safe(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(create_app(
        provider,
        token="t3st-token",
        alpha_council_path=tmp_path / "missing_council.json",
        alpha_workbench_path=tmp_path / "missing_workbench.json",
    ))

    council = client.get("/alpha-council?token=t3st-token").json()
    workbench = client.get("/alpha-workbench?token=t3st-token").json()
    assert council == {"summary": {}, "debates": [], "can_trade": False, "can_promote": False}
    assert workbench == {"summary": {}, "tasks": [], "can_trade": False, "can_promote": False}


def test_no_control_routes_exist(client):
    """Read-only invariant: nothing accepts POST/PUT/DELETE."""
    for method in ("post", "put", "delete"):
        for path in ("/state", "/kill", "/orders", "/config"):
            assert getattr(client, method)(f"{path}?token=t3st-token").status_code in (404, 405)


def test_snapshot_schema_from_wired_world(tmp_path):
    exchange = SimulatedExchange(FillModel(), 500.0)
    exchange.set_quote(SYM, bid=100.0, ask=100.0)
    exchange.submit_order(PaperOrderRequest("o1", SYM, True, 1.0))
    exchange.submit_order(
        PaperOrderRequest("o2", SYM, True, 1.0, order_type="limit", limit_price=99.0)
    )
    tracker = PortfolioTracker(exchange, 500.0)
    kill = KillSwitch(kill_file=tmp_path / "KILL")
    journal = DecisionJournal(tmp_path / "j.jsonl")
    om = OrderManager(PreTradeRiskGateway(RiskConfig(), kill), journal, PaperBroker(exchange))

    snap = build_snapshot(
        mode="paper", live_trading_enabled=False, tracker=tracker,
        exchange=exchange, kill_switch=kill, journal=journal,
        order_manager=om, feed_health=FeedHealth(exchange="test"),
    )
    for field in ("ts", "mode", "live_trading_enabled", "kill_switch_active",
                  "equity", "realized_pnl", "unrealized_pnl", "daily_pnl",
                  "consecutive_losses", "risk_status", "feed_health",
                  "positions", "open_orders", "recent_fills", "last_risk_reject",
                  "last_journal_write"):
        assert field in snap
    assert snap["risk_status"] == "ok"
    assert len(snap["positions"]) == 1
    assert snap["positions"][0]["side"] == "long"
    assert snap["positions"][0]["notional_usd"] == 100.0
    assert snap["open_orders"][0]["client_order_id"] == "o2"
    assert snap["open_orders"][0]["exchange_order_id"].startswith("pex_")
    assert "state_age_ms" in snap["open_orders"][0]
    assert snap["recent_fills"][0]["client_order_id"] == "o1"
    assert snap["recent_fills"][0]["notional_usd"] == pytest.approx(100.02)
    assert snap["recent_fills"][0]["side"] == "buy"

    kill.activate("test")
    snap2 = build_snapshot(
        mode="paper", live_trading_enabled=False, tracker=tracker,
        exchange=exchange, kill_switch=kill, journal=journal,
        order_manager=om, feed_health=FeedHealth(exchange="test"),
    )
    assert snap2["risk_status"] == "kill_switch_active"
    assert snap2["kill_switch_active"] is True


def test_snapshot_marks_restored_position_at_entry_without_quote(tmp_path):
    """Regression (2026-07-07): a resumed session holds a restored position
    BEFORE the feed's first quote — build_snapshot must not KeyError (it
    killed both position-holding lanes); it marks at entry until data."""
    exchange = SimulatedExchange(FillModel(), 500.0)
    exchange.set_quote(SYM, bid=100.0, ask=100.0)
    exchange.submit_order(PaperOrderRequest("x1", SYM, False, 0.5))
    exchange.quotes.clear()  # simulate restart: position restored, no quote yet

    tracker = PortfolioTracker(exchange, 500.0)
    kill = KillSwitch(kill_file=tmp_path / "KILL")
    journal = DecisionJournal(tmp_path / "j.jsonl")
    om = OrderManager(PreTradeRiskGateway(RiskConfig(), kill), journal, PaperBroker(exchange))

    snap = build_snapshot(
        mode="paper", live_trading_enabled=False, tracker=tracker,
        exchange=exchange, kill_switch=kill, journal=journal,
        order_manager=om, feed_health=FeedHealth(exchange="test"),
    )
    pos = snap["positions"][0]
    assert pos["mark_price"] == pos["entry_price"]
    assert pos["unrealized_usd"] == 0.0
