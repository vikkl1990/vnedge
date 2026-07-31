from datetime import UTC, datetime

from vnedge.research.paper_roster_drift import (
    STATE_DEMOTION_STILL_RUNNING,
    STATE_EXPECTED_MISSING,
    STATE_EXPECTED_RUNNING,
    STATE_EXTRA_RUNNING_PAPER,
    build_paper_roster_drift,
)


def test_paper_roster_drift_flags_extra_and_demotion_running_lanes():
    governor = {
        "proposed_roster": {
            "paper_lanes": [
                {
                    "lane_id": "approved_alpha",
                    "strategy_id": "alpha",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                }
            ],
            "demote_to_shadow": [
                {
                    "lane_id": "bad_lane",
                    "strategy_id": "beta",
                    "exchange": "delta_india",
                    "symbol": "BTC/USD:USD",
                    "timeframe": "5m",
                }
            ],
        }
    }
    scanner = {
        "rows": [
            {
                "lane_id": "approved_alpha",
                "mode": "paper (live data)",
                "strategy_id": "alpha",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "state": "WAITING",
            },
            {
                "lane_id": "bad_lane",
                "mode": "paper (live data)",
                "strategy_id": "beta",
                "exchange": "delta_india",
                "symbol": "BTC/USD:USD",
                "timeframe": "5m",
                "state": "WAITING",
            },
            {
                "lane_id": "extra_gamma",
                "mode": "paper (live data)",
                "strategy_id": "gamma",
                "exchange": "bybit",
                "symbol": "SOL/USDT:USDT",
                "timeframe": "15m",
                "state": "FIRING",
            },
        ]
    }

    payload = build_paper_roster_drift(
        governor=governor,
        scanner=scanner,
        activation={"rows": []},
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    states = {row["lane_id"]: row["drift_state"] for row in payload["rows"]}
    assert states["approved_alpha"] == STATE_EXPECTED_RUNNING
    assert states["bad_lane"] == STATE_DEMOTION_STILL_RUNNING
    assert states["extra_gamma"] == STATE_EXTRA_RUNNING_PAPER
    assert payload["summary"]["expected_paper_lanes"] == 1
    assert payload["summary"]["actual_paper_lanes"] == 3
    assert payload["summary"]["extra_paper_lanes"] == 2
    assert payload["summary"]["demotion_queue_running"] == 1
    assert payload["summary"]["drift_detected"] is True
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert "demotion-queue" in payload["operator_answer"]


def test_paper_roster_drift_matches_suffix_lanes_by_signature_and_reports_missing():
    governor = {
        "proposed_roster": {
            "paper_lanes": [
                {
                    "lane_id": "human_name",
                    "strategy_id": "alpha",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                },
                {
                    "lane_id": "missing_lane",
                    "strategy_id": "omega",
                    "exchange": "bybit",
                    "symbol": "XRP/USDT:USDT",
                    "timeframe": "1h",
                },
            ],
        }
    }
    scanner = {
        "rows": [
            {
                "lane_id": "alpha_delta_india_ethusd_5m_paper_observation",
                "mode": "paper (live data)",
                "strategy_id": "alpha",
                "exchange": "delta_india",
                "symbol": "ETH/USD",
                "timeframe": "5m",
            }
        ]
    }

    payload = build_paper_roster_drift(
        governor=governor,
        scanner=scanner,
        activation={"rows": []},
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    states = {row["strategy_id"]: row["drift_state"] for row in payload["rows"]}
    assert states["alpha"] == STATE_EXPECTED_RUNNING
    assert states["omega"] == STATE_EXPECTED_MISSING
    assert payload["summary"]["expected_running"] == 1
    assert payload["summary"]["missing_paper_lanes"] == 1


def test_paper_roster_drift_can_use_activation_when_scanner_is_empty():
    governor = {
        "proposed_roster": {
            "paper_lanes": [
                {
                    "lane_id": "approved_alpha",
                    "strategy_id": "alpha",
                    "exchange": "delta_india",
                    "symbol": "ETH/USD:USD",
                    "timeframe": "5m",
                }
            ],
        }
    }
    activation = {
        "rows": [
            {
                "trial_id": "approved_alpha",
                "activation_state": "PAPER_ONLINE_WAITING",
                "strategy_id": "alpha",
                "exchange": "delta_india",
                "symbol": "ETH/USD:USD",
                "timeframe": "5m",
                "runtime": {"desired_lane_ids": ["approved_alpha"]},
            }
        ]
    }

    payload = build_paper_roster_drift(
        governor=governor,
        scanner={"rows": []},
        activation=activation,
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )

    assert payload["summary"]["actual_paper_lanes"] == 1
    assert payload["summary"]["drift_detected"] is False
    assert payload["rows"][0]["drift_state"] == STATE_EXPECTED_RUNNING
