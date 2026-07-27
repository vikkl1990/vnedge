from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "src/vnedge/dashboard/static/index.html"
APP = ROOT / "src/vnedge/dashboard/app.py"


def _index() -> str:
    return INDEX.read_text()


def test_promote_view_has_joined_operator_lifecycle_console():
    html = _index()
    assert "Operator Lifecycle Console" in html
    assert 'id="lifecycleConsole"' in html
    assert "activation, route doctor, cadence, trade profile, performance, and causality" in html
    assert "Planner inputs are still read-only" in html


def test_dashboard_build_identity_is_runtime_sourced():
    html = _index()
    assert "541e1af" not in html
    assert "(mock)" not in html
    assert 'id="aboutBuild"' in html
    assert 'id="footBuild"' in html
    assert "function renderBuildMeta" in html


def test_dashboard_polled_endpoints_have_app_routes():
    html = _index()
    app = APP.read_text()
    polled: set[str] = set()
    for pattern in (
        r'poll\("([^"]+)"',
        r"poll\('([^']+)'",
        r'fetch\("([^"]+)"',
        r"fetch\('([^']+)'",
    ):
        polled.update(re.findall(pattern, html))
    routes = set(re.findall(r'@app\.(?:get|post|websocket)\("([^"]+)"', app))
    missing = sorted(path for path in polled if path.split("?")[0] not in routes)
    assert not missing
