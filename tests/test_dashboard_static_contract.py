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
    assert "activation, route doctor, cadence, trade profile, performance, survival, and causality" in html
    assert "Planner inputs are still read-only" in html


def test_promote_view_has_read_only_operator_action_queue():
    html = _index()
    assert "Operator Action Queue" in html
    assert 'id="operatorActionQueue"' in html
    assert "function collectLifecycleRows" in html
    assert "function renderOperatorActionQueue" in html
    assert "REPAIR_ROUTE" in html
    assert "REVIEW_PAPER_CANDIDATE" in html
    assert "COLLECT_OUTCOMES" in html
    assert "evidence-ranked only; it cannot approve, promote, or trade" in html


def test_dashboard_has_self_health_console_for_poll_and_ws_truth():
    html = _index()
    assert "Dashboard Self-Health" in html
    assert 'id="dashboardHealth"' in html
    assert "function recordPoll" in html
    assert "function renderDashboardHealth" in html
    assert "st.lastStatus" in html
    assert "wsHealth" in html
    assert "endpoint status, browser poll failures, and payload freshness" in html


def test_dashboard_panel_sections_are_balanced():
    html = _index()
    assert html.count("<section") == html.count("</section>")


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
