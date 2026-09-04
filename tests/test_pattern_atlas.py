"""Pattern Atlas keeps runtime, setup, and evidence claims independent."""

from __future__ import annotations

from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.pattern_atlas import build_pattern_atlas_payload


def _lane(*, lane_id: str, symbol: str, health: str, setup: str) -> dict:
    return {
        "lane_id": lane_id,
        "strategy_id": "session_continuation_realtime_v2",
        "exchange": "delta_india",
        "symbol": symbol,
        "timeframe": "15m",
        "health": health,
        "health_reason": "decision_compute_hard" if health == "blocked" else None,
        "health_reasons": ["decision_compute_hard"] if health == "blocked" else [],
        "health_details": {},
        "candle_status": "ok",
        "candle_age_ms": 0,
        "current_waiting_reason": "regime_route_blocked",
        "last_reject_reason": "quote_duplicate",
        "why_no_fire": "no_signal_observed",
        "last_eval": {"all_failed_gates": ["regime_route_blocked"]},
        "close_to_arm_ms": 1200.0,
        "bar_close_receipt_ms": 400.0,
        "canonical_wait_ms": 300.0,
        "decision_lag_ms": 500.0,
        "quote_ingest_ms": 20.0,
        "acceptance_hold_ms": None,
        "runtime_contract": {"entry_clock": "quote_hold"},
        "lifecycle": {
            "state": setup,
            "armed_current": False,
            "armed_entries": 4,
            "candidates": 2,
            "accepted": 1,
            "rejected": 1,
            "cost_rejected": 0,
            "sizing_rejected": 0,
            "risk_rejected": 0,
            "portfolio_rejected": 0,
            "prerequisite_rejected": 0,
            "resolved": 1,
            "pending": 0,
            "session_state": "eligible",
            "htf_context_age_seconds": 120.0,
            "net_value": -2.5,
            "net_unit": "USD",
            "net_basis": "shadow_booked_execution",
        },
        "shadow_perf": {
            "quotes_seen": 20,
            "quotes_distinct": 18,
            "quote_contract_rejects": 2,
            "quote_overflow_drops": 0,
            "quote_rearms": 3,
            "rejection_reasons": {"quote_duplicate": 2},
        },
    }


def test_pattern_atlas_does_not_merge_symbol_health_or_setup_truth() -> None:
    payload = build_pattern_atlas_payload(
        {
            "generated_at": "2026-09-04T00:00:00Z",
            "source_snapshot_at": "2026-09-04T00:00:00Z",
            "snapshot_state": "fresh",
            "lanes": [
                _lane(lane_id="btc", symbol="BTC/USD:USD", health="blocked", setup="watching"),
                _lane(lane_id="eth", symbol="ETH/USD:USD", health="ok", setup="accepted"),
            ],
        },
        {
            "scanners": [
                {
                    "strategy_id": "session_continuation_realtime_v2",
                    "evidence": "untested",
                    "judgments": 0,
                    "preregistrations": [],
                    "burned_windows": [],
                }
            ]
        },
    )
    pattern = next(row for row in payload["patterns"] if row["id"] == "session-continuation")

    assert pattern["runtime"]["ops_state"] == "blocked"
    assert pattern["runtime"]["setup_state"] == "accepted"
    assert {lane["symbol"] for lane in pattern["runtime"]["lanes"]} == {
        "BTC/USD:USD",
        "ETH/USD:USD",
    }
    btc = next(lane for lane in pattern["runtime"]["lanes"] if lane["lane_id"] == "btc")
    assert btc["ops"]["state"] == "blocked"
    assert btc["setup"]["state"] == "watching"
    assert "decision_compute_hard" in btc["ops"]["reasons"]
    assert "quote_duplicate" in btc["setup"]["reasons"]
    assert pattern["runtime"]["funnel"]["accepted"] == 2
    assert pattern["runtime"]["net_usd"] == -5.0
    assert payload["policy"] == {
        "can_trade": False,
        "can_promote": False,
        "read_only": True,
    }


def test_pattern_atlas_evidence_is_exact_id_and_missing_ids_stay_visible() -> None:
    payload = build_pattern_atlas_payload(
        {"lanes": [], "snapshot_state": "fresh"},
        {
            "scanners": [
                {
                    "strategy_id": "liquidity_sweep_reversal_15m_v1",
                    "evidence": "sealed_fail",
                    "judgments": 1,
                    "preregistrations": ["liquidity_sweep.md"],
                    "burned_windows": [{"verdict": "FAIL"}],
                }
            ]
        },
    )
    sweep = next(row for row in payload["patterns"] if row["id"] == "liquidity-sweep")
    squeeze = next(row for row in payload["patterns"] if row["id"] == "squeeze-expansion")

    assert sweep["evidence"]["state"] == "sealed_fail"
    assert sweep["runtime"]["ops_state"] == "not_rostered"
    assert squeeze["evidence"]["state"] == "untested"
    assert all(not row["catalogued"] for row in squeeze["evidence"]["exact_ids"])


def test_pattern_endpoint_is_auth_gated_and_read_only() -> None:
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow", "lanes": []})
    client = TestClient(create_app(provider, token="pattern-token"))

    assert client.get("/api/patterns").status_code == 401
    response = client.get(
        "/api/patterns", headers={"Authorization": "Bearer pattern-token"}
    )
    assert response.status_code == 200
    assert response.json()["schema"] == "vnedge.pattern_atlas.v2"
    assert response.json()["policy"]["can_trade"] is False
