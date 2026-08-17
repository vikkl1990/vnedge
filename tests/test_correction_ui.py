"""Correction cockpit: policy truth is server-owned and read-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.correction_ui import (
    build_lanes_payload,
    build_risk_payload,
)

NOW = datetime(2026, 8, 16, 14, 0, tzinfo=UTC)


def snapshot() -> dict:
    return {
        "mode": "shadow (live data)",
        "live_trading_enabled": False,
        "kill_switch_active": False,
        "daily_pnl": -3.0,
        "peak_equity": 500.0,
        "last_journal_write": "ok",
        "journal": {
            "available": True,
            "recovery_degraded": True,
            "recovery_error": "malformed record at line 4",
            "quarantine_path": "/logs/lane.journal.jsonl.corrupt",
        },
        "runtime_control": {
            "capital_roster_size": 0,
            "orders_allowed": False,
        },
        "lanes": [
            {
                "lane_id": "measurement_delta_btc",
                "exchange": "delta_india",
                "strategy_id": "measurement_only_v1",
                "mode": "shadow (live data)",
                "symbol": "BTC/USD:USD",
                "timeframe": "1h",
                "feed": "ok",
                "gapped_candles": 1,
                "time_machine": {
                    "health": {"1h": "ok"},
                    "age_ms": {"1h": 4_200.0},
                },
                "latency": {
                    "bar_close_processing_ms": {"p95": 120.0, "n": 20},
                    "decision_lag_ms": {"p95": 4.5, "n": 20},
                },
                "decision_skips": {"forming_1h": 2},
                "cost_profile": "delta_swing",
                "plan_overlay": {"round_trip_bps": 13.0},
                "journal": {"available": True, "recovery_degraded": True},
                "trial_scorecard": {
                    "criteria": [{"name": "daily_loss", "threshold": -10.0, "value": -3.0}]
                },
            },
            {
                "lane_id": "stale_killed_lane",
                "exchange": "binanceusdm",
                "strategy_id": "funding_mean_reversion_v1",
                # A stale runtime must not let a killed strategy look like paper.
                "mode": "paper (live data)",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "1h",
                "feed": "ok",
                "last_fired_ts": (NOW - timedelta(minutes=12)).isoformat(),
                "last_risk_reject": "capital approval missing",
            },
        ],
    }


def test_lanes_are_policy_labelled_and_empty_capital_is_explicit() -> None:
    payload = build_lanes_payload(snapshot(), now=NOW)

    assert payload["measurement_only"] is True
    assert payload["capital_roster_size"] == 0
    assert payload["banner"] == "No capital strategies — measurement only."
    assert payload["can_trade"] is False

    measurement, killed = payload["lanes"]
    assert measurement["eligibility"] == "RESEARCH_ONLY"
    assert measurement["mode"] == "measurement"
    assert measurement["last_signal_age_seconds"] is None
    assert measurement["candle_status"] == "ok"
    assert measurement["candle_age_ms"] == 4200.0
    assert measurement["bar_close_processing_ms"] == 120.0
    assert measurement["decision_lag_ms"] == 4.5
    assert measurement["latency_samples"] == {
        "bar_close": 20, "decision": 20, "required": 20
    }
    assert measurement["arm_skips"] == 2
    assert measurement["last_signal_reason"] == "observe_only"
    assert measurement["cost_profile"] == "delta_swing"
    assert measurement["round_trip_bps"] == 13.0
    assert measurement["why_no_fire"] == ("measurement lane emits no OrderIntent by design")
    assert measurement["health"] == "blocked"  # canonical gap band, not feed-only OK
    assert killed["eligibility"] == "KILLED"
    assert killed["mode"] == "off"
    assert killed["capital"] is False
    assert killed["last_signal_age_seconds"] == 720.0


def test_lane_projection_uses_server_health_bands() -> None:
    snap = snapshot()
    lane = snap["lanes"][0]
    lane["gapped_candles"] = 0
    lane["bands"] = {
        "age": "ok",
        "bar_close_lag": "blocked",
        "decision_lag": "ok",
        "dd": "ok",
    }

    projected = build_lanes_payload(snap, now=NOW)["lanes"][0]

    assert projected["health"] == "blocked"


def test_structure_observe_is_not_mislabeled_as_measurement() -> None:
    snap = snapshot()
    snap["lanes"] = [
        {
            "lane_id": "shadow_observe_binanceusdm_btc",
            "exchange": "binanceusdm",
            "strategy_id": "structure_bos_1h",
            "mode": "shadow (live data)",
            "symbol": "BTC/USDT:USDT",
            "timeframe": "1h",
            "feed": "ok",
            "last_fired_ts": (NOW - timedelta(hours=2)).isoformat(),
            "shadow_perf": {
                "pending_shadow_intents": 1,
                "virtual_net_usd": 3.5,
                "wins": 1,
                "losses": 0,
            },
            "last_reject_reason": "no causal BoS candidate",
        }
    ]
    payload = build_lanes_payload(snap, now=NOW)
    lane = payload["lanes"][0]
    assert payload["banner"] == "SHADOW_OBSERVE · virtual only — no capital strategies."
    assert payload["shadow_observe_lanes"] == 1
    assert lane["mode"] == "shadow"
    assert lane["observation_class"] == "shadow_observe"
    assert lane["capital"] is False
    assert lane["last_signal_age_seconds"] == 7200.0
    assert lane["shadow_perf"]["virtual_net_usd"] == 3.5
    assert lane["last_reject_reason"] == "no causal BoS candidate"


def test_risk_projection_never_hides_gap_journal_or_delta_blocker() -> None:
    payload = build_risk_payload(snapshot())

    assert payload["runtime_mode"] == "measurement"
    assert payload["capital"] == {"enabled": False, "roster_size": 0}
    assert payload["feed"]["status"] == "gap"
    assert payload["journal"]["entries_blocked"] is True
    assert payload["journal"]["quarantine_path"].endswith(".corrupt")
    assert payload["daily_halt"]["used_usd"] == 3.0
    assert payload["daily_halt"]["limit_usd"] == 10.0
    assert payload["daily_halt"]["used_pct_of_peak_equity"] == 0.6
    assert payload["live"]["blocked"] is True
    assert payload["live"]["delta_private_status"] == "not_implemented"
    assert payload["gateway"]["last_reject_reasons"] == [
        {"reason": "capital approval missing", "count": 1}
    ]
    assert payload["gateway"]["window"] == "current_snapshot"
    assert payload["positions"] == {
        "shadow_open": 0,
        "shadow_pending_intents": 0,
        "unresolved_orders": 0,
    }
    assert payload["breaker"] == {
        "loss_streak": 0,
        "active": False,
        "threshold": 3,
    }
    assert payload["live_checklist"]["total"] == 7
    assert payload["live_checklist"]["passed"] == 1
    delta = next(row for row in payload["streams"] if row["exchange"] == "delta_india")
    assert delta["private_stream"] == "not_implemented"
    assert payload["can_trade"] is False


def test_correction_routes_are_authenticated_get_only_projections() -> None:
    provider = SnapshotProvider()
    provider.publish(snapshot())
    app = create_app(provider, token="secret")
    client = TestClient(app)

    for path in ("/api/lanes", "/api/risk/snapshot"):
        assert client.get(path).status_code == 401
        response = client.get(f"{path}?token=secret")
        assert response.status_code == 200
        assert response.json()["read_only"] is True

    order_routes = [
        route
        for route in app.routes
        if "order" in route.path.lower() and set(route.methods or ()) - {"GET", "HEAD"}
    ]
    assert order_routes == []
