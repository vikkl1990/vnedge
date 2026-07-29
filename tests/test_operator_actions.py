from datetime import UTC, datetime

from vnedge.research.operator_actions import (
    ACTION_COLLECT_OUTCOMES,
    ACTION_FIX_SIZE_PROFILE,
    ACTION_REPAIR_ROUTE,
    ACTION_REVIEW_PAPER_CANDIDATE,
    ACTION_RESTORE_CADENCE,
    ACTION_WAIT_FOR_SIGNAL,
    build_operator_actions,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _lane(**overrides):
    row = {
        "lane_key": "stealth|delta_india|eth/usd:usd|5m",
        "trial_id": "stealth_trial",
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
        "strategy_id": "stealth_trail_bbp_v1",
    }
    row.update(overrides)
    return row


def test_operator_actions_repair_route_before_cadence_and_performance():
    payload = build_operator_actions(
        activation={
            "report_id": "paper_lane_activation_v1",
            "rows": [_lane(activation_state="PAPER_ONLINE_WAITING")],
        },
        route={
            "report_id": "paper_route_doctor_v1",
            "rows": [
                _lane(
                    doctor_state="ROUTE_READY_JOURNAL_MISSING",
                    next_action="inspect runner write path",
                )
            ],
        },
        cadence={
            "report_id": "paper_lane_cadence_v1",
            "rows": [_lane(cadence_state="EVAL_STALE", next_action="restore cadence")],
        },
        performance={
            "report_id": "paper_lane_performance_v1",
            "rows": [
                _lane(
                    state="PAPER_ACTIVE_NEGATIVE",
                    closed_trades=4,
                    net_pnl_usd=-2.5,
                )
            ],
        },
        profile={
            "report_id": "trade_profile_matrix_v1",
            "rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")],
        },
        causality={"report_id": "lane_firing_causality_v1", "rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_REPAIR_ROUTE
    assert row["severity"] == "P1"
    assert row["owner"] == "system"
    assert row["action"] == "inspect runner write path"
    assert payload["summary"]["repair_first"] == 1
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_operator_actions_restore_cadence_when_route_is_active():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_ONLINE_WAITING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={
            "rows": [
                _lane(
                    cadence_state="HEARTBEAT_STALE",
                    next_action="restart or inspect the paper runner",
                )
            ]
        },
        performance={"rows": []},
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={"rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_RESTORE_CADENCE
    assert row["action"] == "restart or inspect the paper runner"
    assert payload["summary"]["cadence_repairs"] == 1


def test_operator_actions_profile_fix_precedes_paper_review():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_PROMOTION_CANDIDATE",
                    closed_trades=24,
                    net_pnl_usd=12.0,
                    profit_factor=1.8,
                )
            ]
        },
        profile={
            "rows": [
                _lane(
                    profile="paper",
                    profile_state="PAPER_PROFILE_BLOCKED_BY_RISK",
                    next_action="reduce leverage before route review",
                )
            ]
        },
        causality={"rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_FIX_SIZE_PROFILE
    assert row["owner"] == "operator"
    assert row["action"] == "reduce leverage before route review"
    assert payload["summary"]["profile_fixes"] == 1


def test_operator_actions_promotion_candidate_is_human_review_only():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_PROMOTION_CANDIDATE",
                    closed_trades=24,
                    net_pnl_usd=12.0,
                    profit_factor=1.8,
                    next_action="eligible for human review",
                )
            ]
        },
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={
            "rows": [
                _lane(
                    scanner_state="FIRING",
                    paper_decision={"state": "READY_FOR_PAPER_REVIEW"},
                )
            ]
        },
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_REVIEW_PAPER_CANDIDATE
    assert row["owner"] == "human"
    assert row["action"] == "eligible for human review"
    assert payload["summary"]["paper_review"] == 1
    assert payload["policy"]["human_paper_review_is_not_promotion"] is True


def test_operator_actions_negative_paper_lane_is_collected_not_promoted():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_RUNNING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_SIGNAL_SEEN")]},
        performance={
            "rows": [
                _lane(
                    state="PAPER_ACTIVE_NEGATIVE",
                    closed_trades=6,
                    net_pnl_usd=-4.2,
                    next_action="mine entry/exit failures; do not promote",
                )
            ]
        },
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={"rows": []},
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_COLLECT_OUTCOMES
    assert row["severity"] == "P2"
    assert row["metrics"]["closed_trades"] == 6
    assert row["metrics"]["net_pnl_usd"] == -4.2
    assert payload["summary"]["negative_paper_lanes"] == 1


def test_operator_actions_wait_for_live_signal_when_lane_online():
    payload = build_operator_actions(
        activation={"rows": [_lane(activation_state="PAPER_ONLINE_WAITING")]},
        route={"rows": [_lane(doctor_state="JOURNAL_ACTIVE")]},
        cadence={"rows": [_lane(cadence_state="EVALUATING_NO_SIGNAL")]},
        performance={"rows": []},
        profile={"rows": [_lane(profile="paper", profile_state="PAPER_PROFILE_READY")]},
        causality={
            "rows": [
                _lane(
                    scanner_state="NEAR_TRIGGER",
                    why_no_trade_minute="near trigger; waiting for close confirmation",
                )
            ]
        },
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["bucket"] == ACTION_WAIT_FOR_SIGNAL
    assert row["owner"] == "market"
    assert "near trigger" in row["action"]
