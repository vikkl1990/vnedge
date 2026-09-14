"""Synthetic fixtures exercise plumbing, not evidence of trading edge."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.execution.evidence import DecisionEnvelope
from vnedge.execution.fill_ledger import FillLedger
from vnedge.execution.journal import DecisionJournal
from vnedge.ml.lab_pipeline import (
    LabPlan,
    collect_dataset,
    freeze_dataset,
    load_plan,
    pipeline_summary,
    predict_report,
    register_plan,
    run_experiment,
    temporal_split,
)
from vnedge.ml.ledger_labels import build_ledger_labels, read_records
from vnedge.strategy.arm_evidence import freeze_permission_from_row

START = datetime(2026, 9, 13, tzinfo=UTC)


class Clock(datetime):
    instant = START + timedelta(days=1)

    @classmethod
    def now(cls, tz=None):
        return cls.instant


def envelope(at=START, side="long"):
    snapshot = freeze_permission_from_row(
        {
            "timestamp": at,
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 25.0,
            "quote_volume": 2525.0,
            "trade_count": 40,
            "is_closed": True,
            "data_quality": "ok",
            "candle_source": "canonical_tick_lake",
        },
        decision_timeframe="1m",
        context_timeframes=(),
        allow_long=True,
        allow_short=True,
        reason="fixture",
    )
    return DecisionEnvelope.create(
        strategy_id="ml_fixture_v1",
        symbol="BTC/USD:USD",
        timeframe="1m",
        side=side,
        permission_snapshot=snapshot,
        entry_clock="quote_hold",
    )


def plan(**changes):
    return LabPlan(
        name="Synthetic accounting test",
        cohort={
            "strategy_id": "ml_fixture_v1",
            "exchange": "delta_india",
            "symbol": "BTC/USD:USD",
            "timeframe": "1m",
            "mode": "paper",
            "entry_clock": "quote_hold",
            "cost_profile_id": "fixture_cost_v1",
            "cost_config_sha256": "c" * 64,
            "label_contract": "reconciled_paper_net_positive_v1",
            "feature_fingerprint": "fixture_features",
        },
        features=("ret_1",),
        train_start=START,
        calibration_start=START + timedelta(minutes=221),
        test_start=START + timedelta(minutes=282),
        test_end=START + timedelta(minutes=344),
        embargo_seconds=0,
        **changes,
    )


def add_episode(
    journal,
    ledger,
    i=0,
    *,
    coverage=True,
    clean=True,
    side="long",
    partial=False,
    feature_late=False,
):
    env = envelope(START + timedelta(minutes=i), side)
    t = env.permission_snapshot.decision_bar.close_time
    coid, exit_id = f"entry-{i}", f"exit-{i}"
    Clock.instant = t
    journal.append(
        "order_intent",
        {
            "client_order_id": coid,
            "decision_id": env.decision_id,
            "intent": {"symbol": env.symbol, "reduce_only": False},
            "execution_evidence": {
                "path_id": "kernel_v1",
                "decision_id": env.decision_id,
                "arm_envelope": env.as_dict(),
                "cost_decision": {
                    "approved": True,
                    "cost_profile_id": "fixture_cost_v1",
                    "cost_config_sha256": "c" * 64,
                },
            },
        },
    )
    journal.append(
        "order_intent",
        {"client_order_id": exit_id, "intent": {"symbol": env.symbol, "reduce_only": True}},
    )
    winning = i % 2 == 0
    move = 2.0 if winning else -1.0
    sign = 1 if side == "long" else -1
    base = {
        "mode": "paper",
        "venue": "delta_india",
        "strategy_id": env.strategy_id,
        "symbol": env.symbol,
        "quantity_unit": "base",
        "quantity": 1.0,
        "fee_usd": 0.10,
    }
    ledger.append(
        {
            **base,
            "ts": (t + timedelta(seconds=3)).isoformat(),
            "executed_at": (t + timedelta(seconds=3)).isoformat(),
            "side": "buy" if sign == 1 else "sell",
            "price": 100.0,
            "realized_pnl_usd": 0.0,
            "client_order_id": coid,
            "exchange_seq": 3 * i,
        }
    )
    for part in range(2 if partial else 1):
        quantity = 0.5 if partial else 1.0
        ledger.append(
            {
                **base,
                "quantity": quantity,
                "fee_usd": 0.1 * quantity,
                "ts": (t + timedelta(seconds=20 + part)).isoformat(),
                "side": "sell" if sign == 1 else "buy",
                "executed_at": (t + timedelta(seconds=20 + part)).isoformat(),
                "price": 100.0 + sign * move,
                "realized_pnl_usd": move * quantity,
                "client_order_id": exit_id,
                "exchange_seq": 3 * i + part + 1,
            }
        )
    Clock.instant = t + timedelta(seconds=40)
    journal.append(
        "ml_accounting_checkpoint",
        {
            "fill_tip": ledger.tip_hash,
            "fill_count": ledger.records,
            "clean": clean,
            "recovery_degraded": False,
            "open_positions": [],
            "unresolved_orders": [],
            "orders": {
                coid: {"quantity": 1.0, "fees_usd": 0.1, "state": "filled"},
                exit_id: {"quantity": 1.0, "fees_usd": 0.1, "state": "filled"},
            },
        },
    )
    if coverage:
        journal.append(
            "ml_funding_coverage",
            {
                "symbol": env.symbol,
                "complete": True,
                "start": t.isoformat(),
                "end": (t + timedelta(seconds=30)).isoformat(),
                "source_sha256": "f" * 64,
                "events": [],
            },
        )
    f = {
        "v": 2,
        "fingerprint": "fixture_features",
        "lane": "lane",
        "strategy_id": env.strategy_id,
        "symbol": env.symbol,
        "exchange": "delta_india",
        "timeframe": "1m",
        "side": side,
        "bar_ts": env.bar_open.isoformat(),
        "decision_bar_hash": env.decision_bar_content_hash,
        "decision_id": env.decision_id,
        "backfill": False,
        "decision": "fired",
        "captured_at": (t + timedelta(seconds=1)).isoformat(),
        "ts": (t + timedelta(seconds=5 if feature_late else 2)).isoformat(),
        "features": {"ret_1": 0.01 if winning else -0.01},
    }
    return env, f


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    import vnedge.execution.journal as mod
    import vnedge.ml.lab_pipeline as pipeline

    monkeypatch.setattr(mod, "datetime", Clock)
    monkeypatch.setattr(pipeline, "datetime", Clock)
    Clock.instant = START + timedelta(days=1)
    return DecisionJournal(tmp_path / "lane.journal.jsonl"), FillLedger(
        tmp_path / "lane.fills.jsonl"
    )


def test_closed_fills_are_not_labels_without_funding_coverage(ledgers):
    j, f = ledgers
    add_episode(j, f, coverage=False)
    report = build_ledger_labels(j.path, f.path)
    assert report["labels"] == []
    assert report["rejections"]["settled_funding_coverage_required"] == 1


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("partial", [True, False])
def test_exact_partial_roundtrip_fee_cashflow_and_restart(ledgers, side, partial):
    j, f = ledgers
    env, _ = add_episode(j, f, side=side, partial=partial)
    result = build_ledger_labels(j.path, f.path)
    (label,) = result["labels"]
    assert label["decision_id"] == env.decision_id
    assert label["net_usd"] == pytest.approx(1.8)
    assert label["net_bps"] == pytest.approx(180)
    assert label["target"] == 1
    assert label["performance_eligible"] is False
    assert build_ledger_labels(DecisionJournal(j.path).path, FillLedger(f.path).path) == result


def test_bad_reconciliation_does_not_produce_label(ledgers):
    j, f = ledgers
    add_episode(j, f, clean=False)
    assert build_ledger_labels(j.path, f.path)["labels"] == []


def test_settled_funding_cash_and_rate_must_match_receipt(ledgers):
    j, f = ledgers
    env, _ = add_episode(j, f, coverage=False)
    t = env.permission_snapshot.decision_bar.close_time
    event_at = t + timedelta(seconds=10)
    receipt = {
        "symbol": env.symbol,
        "complete": True,
        "start": t.isoformat(),
        "end": (t + timedelta(seconds=30)).isoformat(),
        "source_sha256": "f" * 64,
        "events": [{"event_id": "settled-1", "ts": event_at.isoformat(), "rate": 0.001}],
    }
    j.append("ml_funding_coverage", receipt)
    assert not build_ledger_labels(j.path, f.path)["labels"]
    j.append(
        "funding_applied",
        {
            "book": "paper",
            "symbol": env.symbol,
            "side": "long",
            "funding_event_id": "settled-1",
            "funding_ts_ms": int(event_at.timestamp() * 1000),
            "funding_rate": 0.001,
            "notional_usd": 100.0,
            "funding_cost_usd": 0.1,
        },
    )
    (label,) = build_ledger_labels(j.path, f.path)["labels"]
    assert label["net_usd"] == pytest.approx(1.7)
    assert label["funding_usd"] == 0.1
    j.append("ml_funding_coverage", {**receipt, "events": []})
    assert not build_ledger_labels(j.path, f.path)["labels"]


def test_no_execution_timestamp_is_not_an_operational_fill(ledgers):
    j, f = ledgers
    add_episode(j, f)
    # Build a valid chain with a legacy missing execution clock.
    rows = read_records(f.path, chain="fills")
    replacement = FillLedger(f.path.with_name("legacy.fills.jsonl"))
    for row in rows:
        replacement.append(
            {k: v for k, v in row.items() if k not in {"seq", "hash", "prev_hash", "executed_at"}}
        )
    assert not build_ledger_labels(j.path, replacement.path)["labels"]


def test_corrupt_full_prefix_quarantines_all_labels(ledgers):
    j, f = ledgers
    add_episode(j, f)
    f.path.write_text(f.path.read_text().replace('"price": 100.0', '"price": 999.0'))
    result = build_ledger_labels(j.path, f.path)
    assert result["state"] == "BLOCKED" and not result["labels"]


def test_late_features_and_missing_selected_features_are_excluded(ledgers):
    j, f = ledgers
    _, row = add_episode(j, f, feature_late=True)
    path = j.path.with_name("lane.features.jsonl")
    path.write_text(json.dumps(row) + "\n")
    result = collect_dataset(j.path.parent, plan())
    assert not result["rows"]
    assert result["rejections"]["feature_unavailable_before_entry"] == 1
    row["ts"] = row["captured_at"]
    row["features"]["ret_1"] = None
    path.write_text(json.dumps(row) + "\n")
    assert collect_dataset(j.path.parent, plan())["rejections"]["incomplete_selected_features"] == 1


def test_duplicate_feature_identity_and_bad_ledger_tail_rejected(ledgers):
    j, f = ledgers
    _, row = add_episode(j, f)
    path = j.path.with_name("lane.features.jsonl")
    path.write_text(
        json.dumps(row) + "\n" + json.dumps({**row, "features": {"ret_1": -999}}) + "\n"
    )
    assert (
        collect_dataset(j.path.parent, plan())["rejections"][
            "missing_or_conflicting_feature_identity"
        ]
        == 1
    )
    with j.path.open("a") as out:
        out.write("{torn")
    assert build_ledger_labels(j.path, f.path)["state"] == "BLOCKED"


def test_temporal_purge_uses_label_availability_not_only_exit():
    p = plan()
    decision = p.calibration_start - timedelta(seconds=10)
    row = {
        "decision_at": decision.isoformat(),
        "entry_at": decision.isoformat(),
        "exit_at": (decision + timedelta(seconds=1)).isoformat(),
        "available_at": (p.calibration_start + timedelta(seconds=2)).isoformat(),
    }
    assert temporal_split([row], p)["train"] == []


def test_preregistration_window_lock_and_no_path_traversal(tmp_path, monkeypatch):
    import vnedge.ml.lab_pipeline as mod

    monkeypatch.setattr(mod, "datetime", Clock)
    Clock.instant = START
    p = plan(purpose="prospective")
    pid = register_plan(tmp_path, p)
    with pytest.raises(ValueError, match="already_reserved"):
        register_plan(tmp_path, p.model_copy(update={"name": "different name"}))
    with pytest.raises(ValueError, match="invalid_artifact"):
        load_plan(tmp_path, "../other")
    Clock.instant = p.test_start
    with pytest.raises(ValueError, match="future"):
        register_plan(tmp_path / "other", p)
    with pytest.raises(ValueError):
        LabPlan.model_validate({**p.model_dump(), "train_start": "2024-07-03T00:00:00Z"})
    assert load_plan(tmp_path, pid)[0] == p


def test_full_synthetic_pipeline_trains_calibrates_and_retains_single_attempt(ledgers, tmp_path):
    j, f = ledgers
    features = []
    for i in range(343):
        _, feature = add_episode(j, f, i=i)
        features.append(feature)
    j.path.with_name("lane.features.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in features)
    )
    Clock.instant = START + timedelta(days=1)
    root = tmp_path / "lab"
    pid = register_plan(root, plan())
    did = freeze_dataset(root, pid, tmp_path)
    result = run_experiment(root, pid, did)
    assert result["status"] == "RESEARCH_COMPLETE"
    assert result["split_rows"] == {"train": 220, "calibration": 61, "test": 62}
    assert 0 <= result["metrics"]["brier"] <= 1
    assert result["can_trade"] is False and result["can_promote"] is False
    with pytest.raises(FileExistsError):
        run_experiment(root, pid, did)
    summary = pipeline_summary(root, tmp_path)
    assert summary["ledger_bound_paper_labels"] == 343
    assert summary["runs_total"] == 1
    assert summary["predictions"] == []  # retrospective predictions are NOT forward
    # Fresh post-training observation is persisted once, with no trade action.
    Clock.instant = START + timedelta(days=2, minutes=1, seconds=3)
    env = envelope(START + timedelta(days=2))
    feature = {
        **features[0],
        "decision_id": env.decision_id,
        "decision_bar_hash": env.decision_bar_content_hash,
        "bar_ts": env.bar_open.isoformat(),
        "captured_at": (Clock.instant - timedelta(seconds=2)).isoformat(),
        "ts": (Clock.instant - timedelta(seconds=1)).isoformat(),
    }
    prediction = predict_report(root, pid, feature, env.as_dict())
    assert 0 <= prediction["probability"] <= 1
    assert prediction["role"] == "report_only_post_arm"
    with pytest.raises(FileExistsError):
        predict_report(root, pid, feature, env.as_dict())
    model = root / "runs" / pid / "research_model.joblib"
    with model.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValueError, match="model_hash_mismatch"):
        predict_report(root, pid, feature, env.as_dict())


def test_status_never_claims_model_from_no_data(tmp_path):
    report = pipeline_summary(tmp_path / "lab", tmp_path / "absent")
    assert report["ledger_bound_paper_labels"] == 0
    assert report["ledger_coverage_complete"] is False
    assert report["runs"] == [] and report["can_trade"] is False


def test_symlink_sources_refused(tmp_path):
    target = tmp_path / "source"
    target.write_text("{}\n")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        read_records(link)


def test_failed_metadata_clock_does_not_change_execution():
    from vnedge.paper.fill_model import FillModel
    from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange

    def broken_clock():
        raise RuntimeError("clock failed")

    venue = SimulatedExchange(FillModel(), execution_clock=broken_clock)
    venue.set_quote("BTC/USD:USD", 100.0, 101.0)
    status = venue.submit_order(PaperOrderRequest("test-order", "BTC/USD:USD", True, 1.0))
    assert status.state == "filled"
    assert venue.get_fills()[0].executed_at is None


def test_forward_worker_reads_arm_before_order_and_quarantines_conflicts(
    ledgers, tmp_path, monkeypatch
):
    from vnedge.research import ml_forward_reports

    j, f = ledgers
    env, feature = add_episode(j, f)
    lane_dir = tmp_path / "forward"
    lane_dir.mkdir()
    journal = DecisionJournal(lane_dir / "fresh.journal.jsonl")
    journal.append("decision_armed", env.as_dict())
    (lane_dir / "fresh.features.jsonl").write_text(json.dumps({**feature, "lane": "fresh"}) + "\n")
    root = tmp_path / "lab"
    pid = register_plan(root, plan())
    monkeypatch.setattr(ml_forward_reports, "datetime", Clock)
    calls = []
    monkeypatch.setattr(ml_forward_reports, "predict_report", lambda *args: calls.append(args))
    report = ml_forward_reports.run_once(root, pid, lane_dir)
    assert report["counts"]["predictions_recorded"] == 1
    assert len(calls) == 1
    journal.append("decision_armed", {**env.as_dict(), "side": "short"})
    calls.clear()
    report = ml_forward_reports.run_once(root, pid, lane_dir)
    assert not calls
    assert report["counts"]["invalid_envelope"] == 1
