from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from vnedge.execution.fill_ledger import verify_chain
from vnedge.research import data_burn
from vnedge.research.strategy_workflow import (
    StrategyWorkflowStore,
    WorkflowError,
    build_strategy_workflow,
)

PARENT = "range_expansion_observer_v3"
CHILD = "range_expansion_observer_v4"


def _register(store: StrategyWorkflowStore, strategy_id: str = PARENT):
    return store.register(
        strategy_id=strategy_id,
        version="1",
        mechanism="closed-bar range expansion",
        timeframes=("1h",),
        symbols=("BTC/USDT:USDT",),
        params={"range_multiple": 1.5, "max_hold_bars": 12},
        code_hash="a" * 64,
        backtest_engine="vnedge_event_backtester",
        engine_version="1",
        now=datetime(2026, 8, 23, tzinfo=UTC),
    )


def test_revision_registry_is_append_only_hash_chained_and_immutable(tmp_path):
    path = tmp_path / "workflow.jsonl"
    store = StrategyWorkflowStore(path, known_strategy_ids={PARENT, CHILD})
    revision = _register(store)

    assert verify_chain(path).ok is True
    assert store.states()[revision.revision_id].revision.params["range_multiple"] == 1.5
    with pytest.raises(WorkflowError, match="already exists"):
        _register(store)

    first = json.loads(path.read_text().splitlines()[0])
    first["revision"]["params"]["range_multiple"] = 0.5
    path.write_text(json.dumps(first) + "\n")
    assert verify_chain(path).ok is False
    with pytest.raises(WorkflowError, match="fails chain verification"):
        StrategyWorkflowStore(path, known_strategy_ids={PARENT, CHILD})


def test_fork_requires_new_reviewed_strategy_id_and_keeps_lineage(tmp_path):
    store = StrategyWorkflowStore(
        tmp_path / "workflow.jsonl",
        known_strategy_ids={PARENT, CHILD},
    )
    parent = _register(store)

    with pytest.raises(WorkflowError, match="new registered strategy_id"):
        store.fork(
            parent_revision_id=parent.revision_id,
            child_strategy_id=PARENT,
            version="2",
        )

    child = store.fork(
        parent_revision_id=parent.revision_id,
        child_strategy_id=CHILD,
        version="1",
        params={"range_multiple": 1.8, "max_hold_bars": 12},
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )

    assert child.parent_revision_id == parent.revision_id
    assert child.strategy_id == CHILD
    assert child.config_hash != parent.config_hash
    assert store.states()[child.revision_id].status == "REGISTERED"


def test_engine_parity_failure_quarantines_revision(tmp_path):
    store = StrategyWorkflowStore(
        tmp_path / "workflow.jsonl",
        known_strategy_ids={PARENT},
    )
    revision = _register(store)

    store.record_parity(
        revision.revision_id,
        "FAIL",
        reference_run_id="run-old",
        current_run_id="run-new",
        max_metric_delta=12.5,
        reason="trade count and net PnL differ after engine upgrade",
    )
    state = store.states()[revision.revision_id]

    assert state.status == "QUARANTINED"
    assert state.parity_status == "FAIL"
    assert "engine upgrade" in state.status_reason
    with pytest.raises(WorkflowError, match="terminal revision"):
        store.record_parity(
            revision.revision_id,
            "PASS",
            reference_run_id="run-old",
            current_run_id="run-third",
            max_metric_delta=0.0,
        )


def test_workflow_joins_lineage_runs_oos_and_never_grants_authority(tmp_path):
    workflow = tmp_path / "workflow.jsonl"
    store = StrategyWorkflowStore(workflow)
    revision = store.register(
        strategy_id=PARENT,
        version="3",
        mechanism="closed-bar range expansion",
        timeframes=("1h",),
        symbols=("BTC/USDT:USDT",),
        params={"range_multiple": 1.5},
        code_hash="a" * 64,
        preregistration="docs/prereg/range_v3.md",
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )
    store.record_parity(
        revision.revision_id,
        "PASS",
        reference_run_id="a",
        current_run_id="b",
        max_metric_delta=0.0,
    )
    feed = tmp_path / "feed.jsonl"
    feed.write_text(
        json.dumps(
            {
                "strategy": PARENT,
                "symbol": "BTC/USDT:USDT",
                "exchange": "binanceusdm",
                "timeframe": "1h",
                "verdict": "PASS",
                "updated": "2026-08-21T00:00:00+00:00",
                "oos_net_usd": 24.0,
                "oos_trades": 42,
                "profit_factor": 1.4,
            }
        )
        + "\n"
    )
    burn = tmp_path / "burn.jsonl"
    data_burn.record_judgment(
        PARENT,
        "BTC/USDT:USDT",
        "binanceusdm",
        "2025-01-01T00:00:00+00:00",
        "2025-06-01T00:00:00+00:00",
        "PASS",
        path=burn,
    )

    payload = build_strategy_workflow(
        workflow_registry_path=workflow,
        feed_path=feed,
        burn_registry_path=burn,
        paper_trials_dir=tmp_path / "paper",
        prereg_dir=tmp_path / "prereg",
        now=datetime(2026, 8, 23, tzinfo=UTC),
    )
    row = next(row for row in payload["revisions"] if row["strategy_id"] == PARENT)

    assert row["stage"] == "SHADOW_OBSERVE"
    assert row["parity_status"] == "PASS"
    assert row["performance"]["trades"] == 42
    assert row["performance"]["sample_qualified"] is True
    assert row["latest_judgment"]["promotable"] is True
    assert row["can_trade"] is False and row["can_promote"] is False
    assert payload["policy"]["can_trade"] is False


def test_missing_registry_still_exposes_registered_strategy_catalog(tmp_path):
    payload = build_strategy_workflow(
        workflow_registry_path=tmp_path / "missing.jsonl",
        feed_path=tmp_path / "missing-feed.jsonl",
        burn_registry_path=tmp_path / "missing-burn.jsonl",
        paper_trials_dir=tmp_path / "missing-paper",
        prereg_dir=tmp_path / "missing-prereg",
        scanner_evidence_path=tmp_path / "missing-scanner-evidence.json",
    )

    assert payload["summary"]["strategies"] > 0
    assert any(row["strategy_id"] == "measurement_only_v1" for row in payload["revisions"])
    assert all(row["can_trade"] is False for row in payload["revisions"])


def test_active_roster_has_explicit_engine_identity_without_faking_parity(tmp_path):
    payload = build_strategy_workflow(
        workflow_registry_path=tmp_path / "missing.jsonl",
        feed_path=tmp_path / "missing-feed.jsonl",
        burn_registry_path=tmp_path / "missing-burn.jsonl",
        paper_trials_dir=tmp_path / "missing-paper",
        prereg_dir=tmp_path / "missing-prereg",
        scanner_evidence_path=tmp_path / "missing-scanner-evidence.json",
    )

    active = {
        "squeeze_expansion_breakout_v4": (
            "quote_acceptance_v1",
            "scanner_exit_v1",
            "1",
        ),
        "range_expansion_realtime_v1": (
            "quote_acceptance_v2",
            "scanner_exit_v1",
            "2",
        ),
        "htf_structure_continuation_realtime_v1": (
            "quote_acceptance_v2",
            "scanner_exit_v1",
            "2",
        ),
        "session_continuation_realtime_v1": (
            "quote_acceptance_v2",
            "scanner_exit_v1",
            "2",
        ),
    }
    rows = {row["strategy_id"]: row for row in payload["revisions"] if row["strategy_id"] in active}

    assert payload["provenance"]["active_roster_revisions"] == 4
    assert payload["summary"]["explicit_revisions"] >= 4
    assert set(rows) == set(active)
    for strategy_id, (decision_engine, exit_engine, engine_version) in active.items():
        row = rows[strategy_id]
        assert row["backtest_engine"]
        assert row["engine_version"] == engine_version
        assert row["params"]["runtime"]["decision_engine"] == decision_engine
        assert row["params"]["runtime"]["exit_engine"] == exit_engine
        assert row["parity_status"] == "NOT_REPORTED"
        assert "ENGINE_PARITY_NOT_REPORTED" in row["governance_flags"]
        assert row["latest_judgment"] is None
        assert row["can_trade"] is False


def test_workflow_keeps_shadow_evidence_separate_from_backtest_metrics(tmp_path):
    scanner_evidence = tmp_path / "scanner-evidence.json"
    scanner_evidence.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-24T02:14:00+00:00",
                "strategies": [
                    {
                        "strategy_id": CHILD,
                        "evaluations": 179,
                        "fires": 2,
                        "virtual_resolved": 1,
                        "virtual_pending": 0,
                        "gross_usd": -18.59,
                        "fees_usd": 4.20,
                        "net_execution_usd": -22.79,
                        "failed_gates": {"session_closed": 148},
                    }
                ],
            }
        )
    )

    payload = build_strategy_workflow(
        workflow_registry_path=tmp_path / "missing.jsonl",
        feed_path=tmp_path / "missing-feed.jsonl",
        burn_registry_path=tmp_path / "missing-burn.jsonl",
        paper_trials_dir=tmp_path / "missing-paper",
        prereg_dir=tmp_path / "missing-prereg",
        scanner_evidence_path=scanner_evidence,
    )
    row = next(row for row in payload["revisions"] if row["strategy_id"] == CHILD)

    assert row["performance"]["after_cost_net_usd"] is None
    assert row["performance"]["trades"] is None
    assert row["shadow_evidence"]["evaluations"] == 179
    assert row["shadow_evidence"]["virtual_resolved"] == 1
    assert row["shadow_evidence"]["net_execution_usd"] == -22.79
    assert row["shadow_evidence"]["promotion_evidence"] is False
    assert payload["summary"]["shadow_evidence"] == 1
