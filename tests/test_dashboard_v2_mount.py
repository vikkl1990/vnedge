"""The v2 React frontend mounts at /app ONLY when a build exists.

Guarantees a production image without a frontend build has no /app route (never
a 500), and the classic dashboard is unaffected either way.
"""

from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app


def test_no_v2_mount_when_dist_absent(tmp_path):
    # point at a non-existent dist → /app must simply not exist (404, not 500)
    client = TestClient(create_app(SnapshotProvider(), token="t", v2_dist_path=tmp_path / "nope"))
    assert client.get("/app/").status_code == 404
    assert client.get("/health").status_code == 200  # classic surface unaffected


def test_v2_spa_served_when_dist_present(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>v2</title><div id=root></div>")
    client = TestClient(create_app(SnapshotProvider(), token="t", v2_dist_path=dist))
    r = client.get("/app/")
    assert r.status_code == 200 and "v2" in r.text
