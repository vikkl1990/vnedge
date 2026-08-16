from __future__ import annotations

from vnedge.runtime.fleet_policy import audit_runtime_snapshot


def test_measurement_only_fleet_is_safe() -> None:
    report = audit_runtime_snapshot(
        {
            "build_sha": "abc123",
            "live_trading_enabled": False,
            "lanes": [
                {
                    "lane_id": "measure_binance_btc",
                    "mode": "shadow",
                    "strategy_id": "measurement_only_v1",
                }
            ],
        },
        expected_build_sha="abc123",
    )
    assert report.safe
    assert report.findings == ()


def test_killed_or_unapproved_capital_lane_is_critical() -> None:
    report = audit_runtime_snapshot(
        {
            "live_trading_enabled": False,
            "lanes": [
                {
                    "lane_id": "old_funding_lane",
                    "mode": "paper",
                    "strategy_id": "funding_mean_reversion_v1",
                },
                {
                    "lane_id": "unapproved_trend",
                    "mode": "live_small",
                    "strategy_id": "trend_continuation_v1",
                },
            ],
        }
    )
    assert not report.safe
    assert {finding.code for finding in report.findings} == {"capital_strategy_denied"}
    assert {finding.lane_id for finding in report.findings} == {
        "old_funding_lane",
        "unapproved_trend",
    }


def test_live_flag_and_build_drift_fail_closed() -> None:
    report = audit_runtime_snapshot(
        {"build_sha": "old", "live_trading_enabled": True, "lanes": []},
        expected_build_sha="new",
    )
    assert not report.safe
    assert {finding.code for finding in report.findings} == {
        "build_mismatch",
        "live_enabled",
    }


def test_declared_capital_count_must_be_auditable() -> None:
    report = audit_runtime_snapshot(
        {
            "runtime_control": {"capital_roster_size": 1},
            "lanes": [{"mode": "shadow", "strategy_id": "measurement_only_v1"}],
        }
    )
    assert not report.safe
    assert report.findings[0].code == "roster_count_inconsistent"
