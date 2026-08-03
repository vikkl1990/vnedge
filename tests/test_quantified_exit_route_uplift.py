from __future__ import annotations

import json
from datetime import UTC, datetime

from vnedge.agent_gateway.jobs import DONE_STATUS, read_job, update_job
from vnedge.research.quantified_exit_route_uplift import (
    QUANTIFIED_EXIT_ROUTE_UPLIFT_ID,
    QuantifiedExitRouteUpliftConfig,
    build_quantified_exit_route_uplift_payload,
    load_quantified_exit_route_uplift_payload,
    publish_quantified_exit_route_uplift,
)


def _arbiter_payload() -> dict:
    return {
        "arbiter_id": "quantified_proof_result_arbiter_v1",
        "generated_at": "2026-08-03T00:00:00+00:00",
        "summary": {
            "gate": {"min_net_bps": 25.0, "min_profit_factor": 1.5, "min_trades": 20}
        },
        "action_queue": [
            {
                "rank": 1,
                "action_id": (
                    "EXIT_ROUTE_UPLIFT|range_volatility_breakout_reversion_v1|"
                    "bybit|SOLUSDTUSDT|4h"
                ),
                "bucket": "EXIT_ROUTE_UPLIFT",
                "next_action": "TEST_TP1_BE_TRAIL_AND_MAKER_FIRST_FILTERS",
                "port_id": "range_volatility_breakout_reversion_v1",
                "exchange": "bybit",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "4h",
                "strategy_id": "quantified_fee_wall_sniper_v1",
                "setup_mode": "breakout_only",
                "adapter": "fee_wall_sniper_breakout",
                "canonical_adapter": True,
                "status": DONE_STATUS,
                "verdict": "POSITIVE_BUT_FEE_WALL_THIN",
                "samples": 42,
                "avg_net_bps": 1.36,
                "required_uplift_bps": 23.64,
                "profit_factor": 1.57,
                "win_rate_pct": 52.0,
            }
        ],
        "can_trade": False,
        "can_promote": False,
    }


def test_exit_route_uplift_seeds_variants_once_and_stays_research_only(tmp_path):
    jobs_dir = tmp_path / "jobs"

    first = build_quantified_exit_route_uplift_payload(
        arbiter_payload=_arbiter_payload(),
        jobs_dir=jobs_dir,
        seed_jobs=True,
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )
    second = build_quantified_exit_route_uplift_payload(
        arbiter_payload=_arbiter_payload(),
        jobs_dir=jobs_dir,
        seed_jobs=True,
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert first["uplift_id"] == QUANTIFIED_EXIT_ROUTE_UPLIFT_ID
    assert first["summary"]["source_actions"] == 1
    assert first["summary"]["experiment_cells"] == 3
    assert first["summary"]["jobs_created"] == 3
    assert second["summary"]["jobs_created"] == 0
    assert second["summary"]["pending_cells"] == 3
    assert first["can_trade"] is False
    assert first["can_promote"] is False

    jobs = [read_job(jobs_dir, path.stem) for path in jobs_dir.glob("agj_*.json")]
    requests = [job["request"] for job in jobs if job is not None]
    assert {req["parameters"]["uplift_variant"] for req in requests} == {
        "tp1_be_trail_taker_v1",
        "profit_lock_20_10_taker_v1",
        "maker_entry_tp1_be_trail_v1",
    }
    maker = next(
        req for req in requests
        if req["parameters"]["uplift_variant"] == "maker_entry_tp1_be_trail_v1"
    )
    assert maker["parameters"]["entry_fee_bps"] == 2.0
    assert maker["parameters"]["exit_fee_bps"] == 5.0
    assert maker["parameters"]["execution_route_model"] == "maker_entry_taker_exit"
    assert maker["live_orders_enabled"] is False


def test_exit_route_uplift_classifies_completed_improvement_and_publish(tmp_path):
    jobs_dir = tmp_path / "jobs"
    build_quantified_exit_route_uplift_payload(
        arbiter_payload=_arbiter_payload(),
        jobs_dir=jobs_dir,
        seed_jobs=True,
        config=QuantifiedExitRouteUpliftConfig(max_source_actions=1),
    )
    job_id = next(
        path.stem
        for path in jobs_dir.glob("agj_*.json")
        if (read_job(jobs_dir, path.stem) or {})["request"]["parameters"]["uplift_variant"]
        == "tp1_be_trail_taker_v1"
    )
    update_job(
        jobs_dir,
        job_id,
        status=DONE_STATUS,
        result={
            "metrics": {
                "num_trades": 20,
                "net_profit_usd": 130.0,
                "profit_factor": 1.62,
                "win_rate_pct": 55.0,
            },
            "can_trade": False,
            "can_promote": False,
            "live_orders_enabled": False,
        },
    )

    payload = build_quantified_exit_route_uplift_payload(
        arbiter_payload=_arbiter_payload(),
        jobs_dir=jobs_dir,
        seed_jobs=False,
    )
    row = next(row for row in payload["rows"] if row["variant_id"] == "tp1_be_trail_taker_v1")

    assert row["avg_net_bps"] == 26.0
    assert row["uplift_bps"] == 24.64
    assert row["verdict"] == "UPLIFT_CLEARS_EXPLORATORY_GATE_REQUIRES_UNTOUCHED_JUDGMENT"
    assert payload["summary"]["clears_exploratory_gate"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False

    out = tmp_path / "quantified_exit_route_uplift_latest.json"
    feed = tmp_path / "quantified_exit_route_uplift_feed.jsonl"
    publish_quantified_exit_route_uplift(payload, out=out, feed=feed)
    loaded = load_quantified_exit_route_uplift_payload(out)

    assert loaded["uplift_id"] == QUANTIFIED_EXIT_ROUTE_UPLIFT_ID
    assert json.loads(feed.read_text(encoding="utf-8").splitlines()[0])["can_trade"] is False
