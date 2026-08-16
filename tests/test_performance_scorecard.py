"""Research scorecard disclosures never dress thin PF/Sharpe up as edge."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.research.performance_scorecard import performance_disclosure


def test_thin_pf_sentinel_and_sharpe_are_hidden():
    row = performance_disclosure(
        {
            "opportunities": 8,
            "profit_factor": 999.0,
            "sharpe": 7.2,
            "sharpe_convention": "daily net returns × sqrt(365)",
            "deflated_sharpe": 0.99,
            "avg_selected_net_bps": 80.0,
            "verdict": "PASS",
        },
        {},
    )
    assert row["metric_state"] == "UNDER_SAMPLED"
    assert row["verdict"] == "UNDER_SAMPLED"
    assert row["source_verdict"] == "PASS"
    assert row["profit_factor"] is None
    assert row["profit_factor_display"] == "hidden"
    assert row["sharpe"] is None
    assert row["deflated_sharpe"] is None
    assert row["deflated_sharpe_pass"] is False
    assert row["oos_net_bps"] == 80.0


def test_qualified_metrics_require_declared_sharpe_convention():
    hidden = performance_disclosure(
        {"trades": 42, "profit_factor": 1.31, "sharpe": 0.9}, {}
    )
    assert hidden["profit_factor"] == 1.31
    assert hidden["sharpe"] is None
    assert hidden["sharpe_reason"] == "annualization convention not reported"

    shown = performance_disclosure(
        {
            "trades": 42,
            "profit_factor": 1.31,
            "sharpe_after_cost": 0.9,
            "sharpe_convention": "daily net returns × sqrt(365); rf=0",
            "deflated_sharpe": 0.96,
            "raw_trials": 100,
            "effective_trials": 12.5,
            "max_drawdown_pct": 6.2,
            "oos_net_bps": 2.1,
        },
        {},
    )
    assert shown["metric_state"] == "SAMPLE_QUALIFIED"
    assert shown["profit_factor"] == 1.31
    assert shown["sharpe"] == 0.9
    assert shown["deflated_sharpe_pass"] is True
    assert shown["raw_trials"] == 100
    assert shown["effective_trials"] == 12.5
    assert shown["trial_count_reason"] is None
    assert shown["max_drawdown_pct"] == 6.2


def test_qualified_no_loss_pf_suppresses_999_numeric_sentinel():
    row = performance_disclosure({"trades": 31, "profit_factor": 999.0}, {})
    assert row["sample_qualified"] is True
    assert row["profit_factor"] is None
    assert row["profit_factor_display"] == "∞"
    assert "sentinel suppressed" in row["profit_factor_reason"]


def test_scorecard_does_not_borrow_aggregate_n_for_best_pf_cell(tmp_path):
    artifact = tmp_path / "fee_wall.json"
    artifact.write_text(
        json.dumps(
            {
                "reports": [
                    {
                        "strategy": "thin_best",
                        "exchange": "bybit",
                        "summary": {
                            "opportunities": 4,
                            "avg_selected_net_bps": 90.0,
                            "profit_factor": 999.0,
                            "sharpe": 9.0,
                            "sharpe_convention": "daily × sqrt(365)",
                            "verdict": "PASS",
                        },
                    },
                    {
                        "strategy": "thin_best",
                        "exchange": "binanceusdm",
                        "summary": {
                            "opportunities": 40,
                            "avg_selected_net_bps": 2.0,
                            "profit_factor": 1.1,
                            "verdict": "WATCH",
                        },
                    },
                ]
            }
        )
    )
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(
        create_app(provider, token="token", fee_wall_forensics_path=artifact)
    )
    row = client.get("/scorecard?token=token").json()["strategies"][0]
    assert row["samples"] == 4
    assert row["samples_total"] == 44
    assert row["metric_state"] == "UNDER_SAMPLED"
    assert row["profit_factor"] is None
    assert row["sharpe"] is None
