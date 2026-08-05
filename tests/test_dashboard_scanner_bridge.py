"""Dashboard bridge for the public-candle MTF/AMF scanner."""

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.scanner_bridge import dashboard_scanner_payload
from vnedge.dashboard.scanner_live import build_scanner_snapshot


def mtf_payload(now: datetime) -> dict:
    fresh = (now - timedelta(minutes=20)).isoformat()
    return {
        "generated_at": now.isoformat(),
        "scanner_id": "mtf_amf_rejection_scanner_v1",
        "mode": "delta_india_public_candles_research_only",
        "symbols": {
            "BTCUSD": {
                "scanner_id": "mtf_amf_rejection_scanner_v1",
                "config": {"chart_timeframe": "1h"},
                "summary": {
                    "alerts": 3,
                    "latest_alert": {
                        "symbol": "BTCUSD",
                        "side": "short",
                        "observed_at": fresh,
                        "l2_confirmation": {
                            "status": "aligned",
                            "context_only": True,
                            "used_for_execution": False,
                        },
                        "can_trade": False,
                        "can_promote": False,
                    },
                },
            },
            "ETHUSD": {
                "scanner_id": "mtf_amf_rejection_scanner_v1",
                "config": {"chart_timeframe": "1h"},
                "summary": {"alerts": 0, "latest_alert": None},
            },
        },
        "errors": {"SOLUSD": "temporary public API error"},
        "can_trade": False,
        "can_promote": False,
    }


def test_mtf_scanner_is_adapted_to_three_safe_dashboard_rows():
    now = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)

    payload = dashboard_scanner_payload(mtf_payload(now), now=now)

    rows = {row["symbol"]: row for row in payload["rows"]}
    assert payload["summary"] == {
        "connected_symbols": 3,
        "firing": 1,
        "waiting": 1,
        "stale": 0,
        "errors": 1,
        "source_age_seconds": 0.0,
    }
    assert rows["BTCUSD"]["state"] == "FIRING"
    assert rows["BTCUSD"]["latest_eval"]["side"] == "short"
    assert rows["BTCUSD"]["latest_eval"]["l2_confirmation"]["status"] == "aligned"
    assert rows["BTCUSD"]["latest_eval"]["l2_confirmation"]["used_for_execution"] is False
    assert rows["ETHUSD"]["state"] == "WAITING"
    assert rows["SOLUSD"]["state"] == "DATA_ERROR"
    assert all(row["can_trade"] is False for row in rows.values())
    assert all(row["can_promote"] is False for row in rows.values())
    assert payload["policy"]["order_route_present"] is False


def test_stale_source_cannot_display_a_firing_scanner():
    now = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
    source = mtf_payload(now - timedelta(minutes=16))

    payload = dashboard_scanner_payload(source, now=now)

    assert payload["summary"]["firing"] == 0
    assert {row["state"] for row in payload["rows"] if row["symbol"] != "SOLUSD"} == {"DATA_STALE"}


def test_existing_realtime_scanner_contract_passes_through_unchanged():
    payload = {
        "mode": "live_observation_not_replay",
        "summary": {"near_trigger": 1},
        "rows": [{"strategy_id": "existing", "state": "NEAR_TRIGGER"}],
        "can_trade": False,
        "can_promote": False,
    }

    assert dashboard_scanner_payload(payload) is payload


def test_token_gated_endpoint_reads_mtf_scanner_file(tmp_path):
    path = tmp_path / "mtf_scanner.json"
    path.write_text(json.dumps(mtf_payload(datetime.now(UTC))))
    provider = SnapshotProvider()
    provider.publish({"mode": "paper (demo replay)"})
    client = TestClient(create_app(provider, token="dashboard-token", realtime_scanner_path=path))

    assert client.get("/realtime-scanner").status_code == 401
    response = client.get("/realtime-scanner?token=dashboard-token")

    assert response.status_code == 200
    assert response.json()["summary"]["connected_symbols"] == 3
    assert response.json()["can_trade"] is False
    assert response.json()["can_promote"] is False


def test_scanner_dashboard_snapshot_has_no_demo_or_execution_state():
    now = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)

    snapshot = build_scanner_snapshot(mtf_payload(now), now=now)

    assert snapshot["mode"] == "research scanner observation"
    assert snapshot["session"]["connected_symbols"] == 3
    assert len(snapshot["lanes"]) == 3
    assert snapshot["positions"] == []
    assert snapshot["open_orders"] == []
    assert snapshot["recent_fills"] == []
    assert snapshot["orders_sent"] == 0
    assert snapshot["live_trading_enabled"] is False
    assert snapshot["can_trade"] is False
    assert snapshot["can_promote"] is False
    assert all(lane["can_trade"] is False for lane in snapshot["lanes"])
