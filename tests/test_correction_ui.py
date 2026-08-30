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
    assert measurement["bar_close_receipt_ms"] == 120.0
    assert measurement["canonical_wait_ms"] is None
    assert measurement["decision_lag_ms"] == 4.5
    assert measurement["latency_samples"] == {
        "bar_close": 20, "canonical_wait": 0, "decision": 20, "required": 20
    }
    assert measurement["arm_skips"] == 2
    assert measurement["last_signal_reason"] == "observe_only"
    assert measurement["cost_profile"] == "delta_swing"
    assert measurement["round_trip_bps"] == 13.0
    assert measurement["why_no_fire"] == ("measurement lane emits no OrderIntent by design")
    assert measurement["health"] == "blocked"  # canonical gap band, not feed-only OK
    assert measurement["health_reason"] == "candle_gap"
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
    assert projected["health_reason"] == "bar_close_lag_hard"
    assert projected["health_reasons"] == ["bar_close_lag_hard"]
    assert projected["health_details"]["bar_close_receipt"] == {
        "p95_ms": 120.0,
        "samples": 20,
        "soft_ms": 500,
        "hard_ms": 2000,
        "recovery_ms": 1500,
        "band": "blocked",
    }


def test_lane_projection_reports_simultaneous_latency_failures() -> None:
    snap = snapshot()
    lane = snap["lanes"][0]
    lane["gapped_candles"] = 0
    lane["arm_blocked"] = "bar_close_lag_hard"
    lane["timeframe"] = "15m"
    lane["latency"] = {
        "bar_close_processing_ms": {"p95": 2_400.0, "n": 101},
        "decision_lag_ms": {"p95": 4_200.0, "n": 101},
    }
    lane["bands"] = {
        "age": "ok",
        "bar_close_lag": "blocked",
        "decision_lag": "blocked",
        "dd": "ok",
    }

    projected = build_lanes_payload(snap, now=NOW)["lanes"][0]

    assert projected["health_reasons"] == [
        "bar_close_lag_hard",
        "decision_lag_hard",
    ]
    assert projected["health_details"]["bar_close_receipt"]["hard_ms"] == 2_000
    assert projected["health_details"]["decision_compute"] == {
        "p95_ms": 4_200.0,
        "samples": 101,
        "soft_ms": 500,
        "hard_ms": 2_500,
        "recovery_ms": 2_000,
        "band": "blocked",
    }


def test_lane_projection_keeps_fresh_candle_separate_from_latency_block() -> None:
    snap = snapshot()
    lane = snap["lanes"][0]
    lane["gapped_candles"] = 0
    lane["arm_blocked"] = "bar_close_lag_hard"

    projected = build_lanes_payload(snap, now=NOW)["lanes"][0]

    assert projected["candle_status"] == "ok"
    assert projected["candle_age_ms"] == 4200.0
    assert projected["health"] == "blocked"
    assert projected["health_reason"] == "bar_close_lag_hard"


def test_lane_projection_distinguishes_current_latency_recovery_from_old_reject() -> None:
    snap = snapshot()
    lane = snap["lanes"][0]
    lane["gapped_candles"] = 0
    lane["last_reject_reason"] = "candle_path:bar_close_lag_hard"
    lane["latency_recovery"] = {
        "bar_close_processing_ms": {
            "state": "recovered",
            "raw_band": "hard",
            "effective_band": "soft",
            "healthy_samples": 5,
            "required_samples": 5,
            "recovery_threshold_ms": 1500,
        }
    }

    projected = build_lanes_payload(snap, now=NOW)["lanes"][0]

    assert projected["last_reject_reason"] == "candle_path:bar_close_lag_hard"
    assert projected["current_waiting_reason"] == "latency_recovered_p95_cooling"
    assert projected["latency_recovery"]["bar_close_processing_ms"]["state"] == "recovered"


def test_structure_observe_is_not_mislabeled_as_measurement() -> None:
    snap = snapshot()
    snap["runtime_control"].update({
        "shadow_observe_strategies": ["structure_bos_1h"],
        "shadow_observe_timeframes": ["1h"],
        "lane_set_hash": "abc123",
    })
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
    assert payload["shadow_observe_strategies"] == ["structure_bos_1h"]
    assert payload["shadow_observe_timeframes"] == ["1h"]
    assert payload["lane_set_hash"] == "abc123"
    assert lane["mode"] == "shadow"
    assert lane["observation_class"] == "shadow_observe"
    assert lane["capital"] is False
    assert lane["last_signal_age_seconds"] == 7200.0
    assert lane["shadow_perf"]["virtual_net_usd"] == 3.5
    assert lane["last_reject_reason"] == "no causal BoS candidate"


def test_quote_lane_lifecycle_does_not_relabel_candidates_as_fires() -> None:
    snap = snapshot()
    snap["snapshot_age_ms"] = 2_500.0
    snap["lanes"] = [
        {
            "lane_id": "shadow_observe_squeeze_btc_5m",
            "exchange": "binanceusdm",
            "strategy_id": "squeeze_expansion_breakout_v4",
            "mode": "shadow (live data)",
            "symbol": "BTC/USDT:USDT",
            "timeframe": "5m",
            "feed": "ok",
            "bands": {
                "age": "ok",
                "bar_close_lag": "ok",
                "decision_lag": "ok",
                "dd": "ok",
            },
            "funnel": {
                "signals": 7,
                "live_signals": 7,
                "shadow_approved": 2,
                "shadow_rejected": 5,
                "risk_rejects": 3,
                "sizing_skips": 1,
            },
            "shadow_perf": {
                "virtual_trades": 2,
                "armed_entries": 287,
                "candidates": 7,
                "approved": 2,
                "rejected": 5,
                "cost_rejected": 3,
                "sizing_rejected": 1,
                "risk_rejected": 1,
                "acceptance_state": "armed_long",
                "virtual_net_usd": -10.81,
            },
            "runtime_contract": {
                "decision_tf": "5m",
                "context_tfs": ["1h", "4h"],
                "context_age_seconds": {"1h": 120.0, "4h": 900.0},
                "entry_clock": "bbo_acceptance",
                "protection_clock": "ticks",
                "decision_engine": "quote_acceptance_v1",
            },
        }
    ]

    payload = build_lanes_payload(snap, now=NOW)
    lane = payload["lanes"][0]

    assert payload["snapshot_state"] == "fresh"
    assert payload["snapshot_age_ms"] == 2500.0
    assert lane["health"] == "ok"
    assert lane["lifecycle"] == {
        "engine_kind": "quote_acceptance",
        "decision_engine": "quote_acceptance_v1",
        "state": "armed",
        "armed_current": True,
        "arm_state": "armed_long",
        "armed_entries": 287,
        "candidates": 7,
        "accepted": 2,
        "rejected": 5,
        "cost_rejected": 3,
        "sizing_rejected": 1,
        "risk_rejected": 1,
        "portfolio_rejected": 0,
        "prerequisite_rejected": 0,
        "fires": None,
        "resolved": 2,
        "pending": 0,
        "session_state": "eligible",
        "htf_context_age_seconds": 900.0,
        "net_value": -10.81,
        "net_unit": "USD",
        "net_basis": "shadow_booked_execution",
    }


def test_next_open_lane_keeps_real_closed_bar_fire_count() -> None:
    snap = snapshot()
    snap["snapshot_age_ms"] = 20_000.0
    snap["lanes"] = [
        {
            "lane_id": "shadow_observe_structure_btc_1h",
            "exchange": "binanceusdm",
            "strategy_id": "structure_bos_1h",
            "mode": "shadow (live data)",
            "symbol": "BTC/USDT:USDT",
            "timeframe": "1h",
            "feed": "ok",
            "bands": {
                "age": "ok",
                "bar_close_lag": "ok",
                "decision_lag": "ok",
                "dd": "ok",
            },
            "funnel": {"live_signals": 3, "shadow_approved": 2},
            "runtime_contract": {
                "entry_clock": "next_open",
                "decision_engine": "base_strategy_next_open_v1",
            },
        }
    ]

    payload = build_lanes_payload(snap, now=NOW)
    lifecycle = payload["lanes"][0]["lifecycle"]

    assert payload["snapshot_state"] == "stale"
    assert lifecycle["engine_kind"] == "next_open"
    assert lifecycle["fires"] == 3
    assert lifecycle["accepted"] == 2


def test_sizing_contract_is_only_exposed_for_actionable_virtual_or_paper_rows() -> None:
    snap = snapshot()
    sizing = {
        "starting_equity_usd": 500.0,
        "fixed_margin_usd": 100.0,
        "max_leverage": 30,
    }
    snap["lanes"][0]["sizing_profile"] = sizing
    snap["lanes"].append(
        {
            "lane_id": "shadow_observe_binanceusdm_btc",
            "exchange": "binanceusdm",
            "strategy_id": "structure_bos_1h",
            "mode": "shadow (live data)",
            "symbol": "BTC/USDT:USDT",
            "timeframe": "1h",
            "feed": "ok",
            "equity": 500.0,
            "sizing_profile": sizing,
        }
    )

    payload = build_lanes_payload(snap, now=NOW)
    measurement = next(
        row for row in payload["lanes"] if row["observation_class"] == "measurement"
    )
    observer = next(
        row for row in payload["lanes"] if row["observation_class"] == "shadow_observe"
    )

    assert measurement["sizing_profile"] is None
    assert observer["sizing_profile"] == sizing
    assert payload["portfolio"]["measurement_nominal_usd"] == 0.0
    assert payload["portfolio"]["shadow_purse_usd"] == 500.0


def test_risk_mode_reports_active_shadow_observer() -> None:
    snap = snapshot()
    snap["lanes"].append(
        {
            "lane_id": "shadow_observe_binanceusdm_btc",
            "exchange": "binanceusdm",
            "strategy_id": "structure_bos_1h",
            "mode": "shadow (live data)",
            "symbol": "BTC/USDT:USDT",
            "timeframe": "1h",
            "feed": "ok",
        }
    )

    payload = build_risk_payload(snap)

    assert payload["runtime_mode"] == "shadow"
    assert payload["capital"] == {"enabled": False, "roster_size": 0}


def test_risk_projection_separates_feed_from_gap_journal_and_delta_blocker() -> None:
    payload = build_risk_payload(snapshot())

    assert payload["runtime_mode"] == "measurement"
    assert payload["capital"] == {"enabled": False, "roster_size": 0}
    # The public transport is healthy; the candle gap remains visible on the
    # lane and must not be relabelled as a websocket/feed failure.
    assert payload["feed"] == {"status": "healthy", "label": "healthy"}
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
