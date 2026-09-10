"""Explicit public measurements must never grant operator/agent authority."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vnedge.agent_gateway.auth import AgentToken, AgentTokenStore
from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.session import SessionIssuer


def make_client(tmp_path, monkeypatch, enabled="1", issuer=None):
    monkeypatch.setenv("DASHBOARD_PUBLIC_READ_ONLY", enabled)
    monkeypatch.setenv("DASHBOARD_ALLOW_QUERY_TOKEN", "0")
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "can_trade": False})
    return TestClient(
        create_app(
            provider,
            token="operator-test-secret",
            session_issuer=issuer,
            settings_path=tmp_path / "settings.sqlite",
            settings_audit_path=tmp_path / "audit.jsonl",
            agent_jobs_dir=tmp_path / "jobs",
            agent_audit_path=tmp_path / "agent-audit.jsonl",
            agent_token_store=AgentTokenStore(
                [AgentToken.from_secret(name="test-agent", token="agent-test-secret")]
            ),
        ),
        base_url="https://testserver",
    )


@pytest.mark.parametrize("enabled", ["0", "", "true", "unexpected"])
def test_public_view_requires_exact_opt_in(tmp_path, monkeypatch, enabled):
    client = make_client(tmp_path, monkeypatch, enabled)
    assert client.get("/state").status_code == 401
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws"):
        pass


def test_public_http_and_socket_and_cookie_refresh(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    assert client.get("/state").json()["can_trade"] is False
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["mode"] == "shadow"
    assert client.post("/auth/session").status_code == 401
    response = client.get("/whoami")
    assert response.status_code == 200
    assert response.json()["role"] == "viewer"
    assert response.json()["permissions"] == ["view"]
    assert response.json()["expires_at"]
    assert "operator-test-secret" not in response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.post("/auth/session/refresh").status_code == 403
    refreshed = client.post(
        "/auth/session/refresh", headers={"X-VNEDGE-CSRF": client.cookies["vnedge_csrf"]}
    )
    assert refreshed.status_code == 200 and refreshed.json()["role"] == "viewer"
    assert client.get("/research-pipeline").status_code == 200


def test_public_cannot_access_settings_or_queue_backtest(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    client.get("/whoami")
    assert client.get("/api/settings/profile").status_code == 403
    assert client.put("/api/settings/profile", json={"display_name": "attacker", "timezone": "UTC"}).status_code == 403
    assert client.get("/api/settings/exchanges").status_code == 403
    assert client.get("/api/agent/v1/whoami").status_code == 401
    request = {
        "strategy_id": "trend_continuation_v1",
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "1h",
        "initial_capital_usd": 1000,
        "commission_bps": 5,
        "slippage_bps": 1,
        "strict_mode": True,
        "live_orders_enabled": False,
        "parameters": {},
    }
    assert (
        client.post(
            "/backtest-lab/runs",
            json=request,
            headers={"X-VNEDGE-CSRF": client.cookies["vnedge_csrf"]},
        ).status_code
        == 403
    )
    assert not list((tmp_path / "jobs").glob("*.json"))
    operator = client.get("/whoami", headers={"Authorization": "Bearer operator-test-secret"})
    assert operator.json()["role"] == "operator"


def test_disable_revokes_public_cookie_with_same_signing_key(tmp_path, monkeypatch):
    issuer = SessionIssuer(b"stable-test-signing-key")
    public = make_client(tmp_path, monkeypatch, issuer=issuer)
    public.get("/whoami")
    private = make_client(tmp_path, monkeypatch, "0", issuer=issuer)
    private.cookies.update(public.cookies)
    assert private.get("/whoami").status_code == 401
    assert (
        private.post(
            "/auth/session/refresh", headers={"X-VNEDGE-CSRF": public.cookies["vnedge_csrf"]}
        ).status_code
        == 401
    )
    with pytest.raises(WebSocketDisconnect), private.websocket_connect("/ws"):
        pass


def test_public_mode_still_requires_operator_credential(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PUBLIC_READ_ONLY", "1")
    with pytest.raises(ValueError, match="no token, no dashboard"):
        create_app(SnapshotProvider())
