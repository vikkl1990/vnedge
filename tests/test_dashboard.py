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
    assert "Commercial Operator Workstation" in html
    assert "workspace navigation" in html
    assert "Quant Command Deck" in html
    assert "why no trade console" in html
    assert "Maker fee wall" in html
    assert "Route Radar" in html
    assert "Signal Pressure" in html
    assert "operator actionability matrix" in html
    assert "Multi-exchange Lane Matrix" in html
    assert "Fee Wall" in html
    assert "Signal Pressure &amp; Trade Journal" in html
    assert "scanner-style hot/cold pressure" in html
    assert "Alpha Council &amp; Proof Queue" in html
    assert "Persistent Proof Queue" in html
    assert "LIVE ARMED" in html


def test_dashboard_shell_preserves_operator_instruments(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    # Acceptance guard for the #116 fix-forward restyle: visual polish must
    # keep the operator instruments wired into the current dashboard.
    assert "What changed" in html
    assert 'id="funnelBody"' in html
    assert 'id="tradeJournalBody"' in html
    assert 'id="mode"' in html and 'id="symbol"' in html and 'id="strategy"' in html
    assert 'id="risk"' in html and 'id="kill"' in html and 'id="conn"' in html
    assert 'id="connectionsBoard"' in html
    assert 'id="zoneTradingFloor"' in html
    assert 'id="zoneResearchLab"' in html
    assert 'id="zoneInfrastructure"' in html
    assert 'className="v-badge"' in html
    assert "virtual PnL -- shadow lane, no real orders" in html
    assert 'id="laneHealthBadge"' in html


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


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _incident_world(tmp_path):
    """alerts.jsonl + one lane journal with a mix of incident and routine kinds."""
    alerts = tmp_path / "alerts.jsonl"
    _write_jsonl(alerts, [
        {"ts": "2026-07-10T02:00:00+00:00", "rule_id": "feed_stale",
         "severity": "critical", "message": "feed stale: 130s since last event"},
        {"ts": "2026-07-10T03:00:00+00:00", "rule_id": "new_fill",
         "severity": "info", "message": "fill #1"},  # notification, not incident
        {"ts": "2026-07-10T04:00:00+00:00", "rule_id": "loss_streak",
         "severity": "warning", "message": "3 consecutive losing round trips"},
    ])
    _write_jsonl(tmp_path / "btc_lane.journal.jsonl", [
        {"ts": "2026-07-10T01:00:00+00:00", "kind": "order_intent",
         "payload": {"client_order_id": "x"}},  # routine, not incident
        {"ts": "2026-07-10T05:00:00+00:00", "kind": "reconciliation_fail_closed",
         "payload": {"mismatches": ["position drift"]}},
        {"ts": "2026-07-10T00:30:00+00:00", "kind": "orphaned_paper_position",
         "payload": {"symbol": SYM}},
        {"ts": "2026-07-10T00:15:00+00:00", "kind": "plan_restore_rejected",
         "payload": {"reason": "wrong symbol"}},
        {"ts": "2026-07-10T06:00:00+00:00", "kind": "emergency_flatten_started",
         "payload": {"flatten_id": "f1"}},
    ])
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    return TestClient(create_app(
        provider, token="t3st-token", alerts_path=alerts, journal_dir=tmp_path
    ))


def test_incidents_requires_token(tmp_path):
    client = _incident_world(tmp_path)
    assert client.get("/incidents").status_code == 401
    assert client.get("/incidents?token=wrong").status_code == 401


def test_incidents_merges_orders_and_maps_severity(tmp_path):
    client = _incident_world(tmp_path)
    incidents = client.get("/incidents?token=t3st-token").json()

    # merged from both sources, reverse-chronological
    stamps = [i["ts"] for i in incidents]
    assert stamps == sorted(stamps, reverse=True)
    by_source = {i["source"]: i for i in incidents}
    assert "alert:feed_stale" in by_source
    assert "journal:btc_lane" in {i["source"] for i in incidents}

    # routine records are excluded from the incident timeline
    assert not any("new_fill" in i["source"] for i in incidents)
    assert not any(i["message"].startswith("order_intent") for i in incidents)

    # severity mapping: journal kinds carry hard-coded severities
    sev = {i["message"].split(" — ")[0]: i["severity"] for i in incidents
           if i["source"].startswith("journal:")}
    assert sev["reconciliation_fail_closed"] == "critical"
    assert sev["orphaned_paper_position"] == "warning"
    assert sev["plan_restore_rejected"] == "warning"
    assert sev["emergency_flatten_started"] == "critical"

    # every incident links a runbook anchor
    assert all(i["runbook"].startswith("/runbooks#") for i in incidents)
    kill = next(i for i in incidents if "emergency_flatten" in i["message"])
    assert kill["runbook"] == "/runbooks#kill-switch-and-flatten"


def test_incidents_limit_param_and_missing_files(tmp_path):
    client = _incident_world(tmp_path)
    assert len(client.get("/incidents?token=t3st-token&limit=2").json()) == 2
    assert client.get("/incidents?token=t3st-token&limit=nope").status_code == 400

    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    bare = TestClient(create_app(
        provider, token="t3st-token",
        alerts_path=tmp_path / "missing" / "alerts.jsonl",
        journal_dir=tmp_path / "missing",
    ))
    assert bare.get("/incidents?token=t3st-token").json() == []


def test_runbooks_route_is_auth_gated_and_anchored(client):
    assert client.get("/runbooks").status_code == 401
    r = client.get("/runbooks?token=t3st-token")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # anchors the incident links point at, from the real docs/RUNBOOKS.md
    for anchor in ("kill-switch-and-flatten", "reconciliation-fail-closed",
                   "orphaned-paper-position", "plan-restore-rejected",
                   "general-triage"):
        assert f"id='{anchor}'" in r.text
    assert "NEVER auto-resets" in r.text


def test_runbooks_custom_path_and_missing_file(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    doc = tmp_path / "RUNBOOKS.md"
    doc.write_text("# Title\n\n## My Incident Type\n\n- check <thing> & act\n")
    client = TestClient(create_app(
        provider, token="t3st-token", runbooks_path=doc
    ))
    r = client.get("/runbooks?token=t3st-token")
    assert "id='my-incident-type'" in r.text
    assert "&lt;thing&gt; &amp; act" in r.text  # body is escaped, not interpreted

    gone = TestClient(create_app(
        provider, token="t3st-token", runbooks_path=tmp_path / "nope.md"
    ))
    assert gone.get("/runbooks?token=t3st-token").status_code == 404


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
