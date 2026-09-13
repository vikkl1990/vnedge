import json
from vnedge.dashboard.service_status import service_status


def test_host_report_is_freshness_qualified_and_read_only(tmp_path):
    path = tmp_path / "report.json"
    assert service_status(path)["status"] == "unknown"
    path.write_text(json.dumps({"generated_at": 1000, "services": {
        "dashboard-tls": {"running": True, "health": "healthy", "action": "none", "secret": "not-public"}
    }}))
    fresh = service_status(path, now=1050)
    assert fresh["status"] == "fresh"
    assert fresh["can_trade"] is False
    assert "secret" not in fresh["services"][0]
    stale = service_status(path, now=1300)
    assert stale["status"] == "stale"
    assert stale["services"][0]["health"] == "unknown"
    assert service_status(path, now=900)["status"] == "unknown"


def test_corrupt_report_fails_visible_not_server_error(tmp_path):
    path = tmp_path / "report.json"
    for raw in ('{', 'null', '{"generated_at":NaN}', '{"generated_at":0,"services":[]}'):
        path.write_text(raw)
        assert service_status(path, now=1)["status"] == "unknown"


def test_service_endpoint_preserves_authentication(monkeypatch):
    from fastapi.testclient import TestClient
    from vnedge.dashboard.app import SnapshotProvider, create_app
    monkeypatch.setenv("DASHBOARD_PUBLIC_READ_ONLY", "0")
    client = TestClient(create_app(SnapshotProvider(), token="test-service-token"))
    assert client.get("/api/services").status_code == 401
    response = client.get("/api/services", headers={"Authorization": "Bearer test-service-token"})
    assert response.status_code == 200
    assert response.json()["can_trade"] is False
    assert client.post("/api/services", json={"restart": "all"}).status_code == 405
