"""Dashboard — auth gates, snapshot schema, read-only surface."""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from vnedge.config.risk_config import RiskConfig
from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.auth import DashboardUser, TokenStore, parse_users_env
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


# ---------------------------------------------------------------------------
# Per-user auth: token store, roles, expiry, back-compat (auth.py)
# ---------------------------------------------------------------------------


def test_parse_users_env_roles_and_expiry():
    users = parse_users_env(
        "alice:tok-a:viewer;bob:tok-b:OPERATOR:2027-01-01T00:00:00+00:00"
    )
    assert [u.name for u in users] == ["alice", "bob"]
    assert users[0].role == "viewer" and users[0].expires_at is None
    assert users[1].role == "operator"  # role is case-insensitive
    assert users[1].expires_at is not None
    assert users[1].expires_at.tzinfo is not None
    assert users[1].expires_at.year == 2027


def test_parse_users_env_naive_expiry_assumed_utc():
    (user,) = parse_users_env("carol:tok-c:viewer:2027-06-01T12:00:00")
    assert user.expires_at == datetime(2027, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_users_env_malformed_entries_skipped_loudly(caplog):
    raw = (
        "good:tok-good:viewer"          # valid
        ";too-short"                    # < 3 fields
        ";badrole:tok-role:admin"       # unknown role
        ";:tok-empty:viewer"            # empty name
        ";badexp:tok-exp:viewer:not-a-date"  # unparseable expiry
        ";good:tok-dupe:operator"       # duplicate name
        ";;"                            # blank entries ignored quietly
    )
    with caplog.at_level(logging.WARNING, logger="vnedge.dashboard.auth"):
        users = parse_users_env(raw)
    assert [u.name for u in users] == ["good"]
    skipped = [r for r in caplog.records if "skipped" in r.getMessage()]
    assert len(skipped) == 5  # every malformed entry is called out loudly
    # Token values must never appear in logs.
    logged = " ".join(r.getMessage() for r in caplog.records)
    for secret in ("tok-good", "tok-role", "tok-empty", "tok-exp", "tok-dupe"):
        assert secret not in logged


def test_token_store_from_env_back_compat_single_token():
    store = TokenStore.from_env({"DASHBOARD_TOKEN": "legacy-secret"})
    assert len(store) == 1
    result = store.authenticate("legacy-secret")
    assert result.authorized
    assert result.name == "operator" and result.role == "operator"
    assert result.expires_at is None
    assert not store.authenticate("wrong").authorized


def test_token_store_from_env_merges_users_and_legacy_token():
    store = TokenStore.from_env({
        "DASHBOARD_USERS": "alice:tok-a:viewer",
        "DASHBOARD_TOKEN": "legacy-secret",
    })
    assert len(store) == 2
    assert store.authenticate("tok-a").name == "alice"
    assert store.authenticate("legacy-secret").name == "operator"


def test_token_store_expired_token_rejected_with_reason(caplog):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    store = TokenStore([DashboardUser("eve", "tok-e", "viewer", expires_at=past)])
    with caplog.at_level(logging.WARNING, logger="vnedge.dashboard.auth"):
        result = store.authenticate("tok-e")
    assert not result.authorized
    assert result.name == "eve"
    assert "expired" in (result.reason or "")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "eve" in logged and "tok-e" not in logged


def test_token_store_future_expiry_still_valid():
    future = datetime.now(timezone.utc) + timedelta(days=30)
    store = TokenStore([DashboardUser("dan", "tok-d", "operator", expires_at=future)])
    result = store.authenticate("tok-d")
    assert result.authorized and result.name == "dan" and result.expires_at == future


def test_token_store_auth_events_logged_without_tokens(caplog):
    store = TokenStore([DashboardUser("alice", "tok-a", "viewer")])
    with caplog.at_level(logging.INFO, logger="vnedge.dashboard.auth"):
        assert store.authenticate("tok-a").authorized
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "alice" in logged and "viewer" in logged and "tok-a" not in logged


# ---------------------------------------------------------------------------
# Per-user auth wired into the app: identity header, expiry, WS
# ---------------------------------------------------------------------------


def _multi_user_client() -> TestClient:
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "equity": 500.0})
    store = TokenStore([
        DashboardUser("alice", "tok-alice", "viewer"),
        DashboardUser("bob", "tok-bob", "operator"),
        DashboardUser(
            "expired-carl", "tok-carl", "viewer",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ),
    ])
    return TestClient(create_app(provider, token_store=store))


def test_multi_user_tokens_accepted_with_identity_header():
    client = _multi_user_client()
    r = client.get("/state", headers={"Authorization": "Bearer tok-alice"})
    assert r.status_code == 200
    assert r.headers["X-Dashboard-User"] == "alice"
    r2 = client.get("/state?token=tok-bob")
    assert r2.status_code == 200
    assert r2.headers["X-Dashboard-User"] == "bob"


def test_multi_user_wrong_token_rejected():
    client = _multi_user_client()
    r = client.get("/state?token=not-a-token")
    assert r.status_code == 401
    assert r.json()["detail"] == "missing or invalid token"
    assert "X-Dashboard-User" not in r.headers


def test_expired_token_rejected_with_clear_reason_over_http():
    client = _multi_user_client()
    r = client.get("/state?token=tok-carl")
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


def test_identity_header_on_all_data_routes():
    client = _multi_user_client()
    for path in ("/state", "/history", "/research", "/alpha-council", "/alpha-workbench"):
        r = client.get(f"{path}?token=tok-alice")
        assert r.status_code in (200, 503), path
        assert r.headers["X-Dashboard-User"] == "alice", path


def test_back_compat_shared_token_is_operator_identity(client):
    r = client.get("/state?token=t3st-token")
    assert r.status_code == 200
    assert r.headers["X-Dashboard-User"] == "operator"


def test_websocket_multi_user_snapshot_carries_connection_count():
    client = _multi_user_client()
    with client.websocket_connect("/ws?token=tok-alice") as ws:
        payload = ws.receive_json()
        assert payload["equity"] == 500.0
        assert payload["dashboard_connections"] == 1


def test_websocket_expired_token_rejected():
    client = _multi_user_client()
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=tok-carl") as ws:
            ws.receive_json()


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
