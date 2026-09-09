from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.research import governed_ai_research as governed
from vnedge.research.continuous_ai_pipeline import materialize_next_blueprint
from vnedge.research.experiment_packet import ExperimentSpec, digest, falsify, persist_once, preflight
from vnedge.research.universe import ResearchTarget


def bars(n=130):
    rows = []
    for i in range(n):
        stamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i)
        row = dict(timestamp=stamp, open=100.0, high=102.0, low=98.0, close=101.0,
                   volume=1.0, quote_volume=101.0, trade_count=1, is_closed=True,
                   data_quality="ok", candle_source="canonical_tick_lake", coverage_ok=True,
                   exchange="delta_india", symbol="BTC/USD:USD", timeframe="1h")
        row["content_sha256"] = bar_content_sha256(
            row, open_time=stamp, close_time=stamp + timedelta(hours=1), source="canonical_tick_lake")
        rows.append(row)
    return pd.DataFrame(rows)


def spec(**kwargs):
    return ExperimentSpec(strategy_id="ai_test", source_sha256="a" * 64,
                          claim="Closed-bar recovery after an excursion.",
                          invalidation="Reject if rolling OOS gates fail.",
                          cost_profile_id="delta_swing", train_bars=110, test_bars=10, **kwargs)


def check(frame, contract=None):
    return preflight(contract or spec(), frame, warmup_bars=103,
                     now=datetime(2026, 9, 9, tzinfo=UTC),
                     source_sha256="a" * 64, strategy_id="ai_test")


@pytest.mark.parametrize("mutation,reason", [
    (lambda f: f.assign(is_closed=False), "decision_row_not_closed"),
    (lambda f: f.assign(content_sha256="a" * 64), "decision_row_content_hash_mismatch"),
    (lambda f: f.assign(symbol="ETH/USD:USD"), "symbol_identity_missing_or_mismatch"),
    (lambda f: f.drop(columns="symbol"), "symbol_identity_missing_or_mismatch"),
    (lambda f: f.rename(columns={"timestamp": "open_time"}), "decision_timestamp_column_missing"),
    (lambda f: f.assign(coverage_ok=False), "coverage_unproven"),
    (lambda f: f.drop(index=10), "non_consecutive_bars"),
    (lambda f: f.iloc[::-1], "non_consecutive_bars"),
    (lambda f: f.head(20), "history_insufficient"),
])
def test_preflight_failures_are_not_economic_rejects(mutation, reason):
    result = check(mutation(bars()))
    assert result["status"] == "NOT_TESTABLE"
    assert reason in result["failures"]


def test_unsupported_clocks_and_context_do_not_get_fake_backtests():
    assert check(bars())["status"] == "READY_TO_TEST"
    assert "lane_bbo_replay_required" in check(bars(), spec(entry_clock="quote_hold"))["failures"]
    assert "bound_context_replay_required" in check(bars(), spec(context_timeframes=("4h",)))["failures"]


def test_exact_volume_and_future_rows_fail_closed():
    frame = bars()
    frame.loc[0, "volume"] = 0.0
    row = frame.iloc[0].to_dict()
    frame.loc[0, "content_sha256"] = bar_content_sha256(
        row, open_time=row["timestamp"], close_time=row["timestamp"] + timedelta(hours=1),
        source="canonical_tick_lake")
    assert "exact_volume_window_not_ready" in check(frame, spec(exact_volume=True))["failures"]
    result = preflight(spec(), bars(), warmup_bars=103, now=datetime(2025, 1, 1, tzinfo=UTC),
                       source_sha256="a" * 64, strategy_id="ai_test")
    assert "future_bar" in result["failures"]


def setup_run(tmp_path, frame):
    strategy_dir = tmp_path / "strategies"
    materialize_next_blueprint(strategy_dir)
    contract_path = next(strategy_dir.glob("*.experiment.json"))
    contract = json.loads(contract_path.read_text())
    contract.update(train_bars=110, test_bars=10)
    contract_path.write_text(json.dumps(contract))

    class Store:
        def read_candles(self, *args):
            return frame

    return Store(), strategy_dir, tmp_path / "evidence"


def test_packet_persisted_before_replay_and_results_retain_audit(tmp_path, monkeypatch):
    store, strategy_dir, out = setup_run(tmp_path, bars())

    def evaluate(*args, **kwargs):
        packet_file = next((out / "packets").glob("*.json"))
        packet = json.loads(packet_file.read_text())
        packet_id = packet.pop("packet_id")
        from vnedge.research.experiment_packet import json_bytes
        assert digest(json_bytes(packet)) == packet_id
        assert packet["costs"]["booked_round_bps"] == pytest.approx(15.8)
        assert kwargs["config"].cost_profile == "delta_swing"
        assert args[3] is None  # declared funding excluded, never silently mixed
        assert len(list((out / "attempts").glob("*.started.json"))) == 1
        return {"verdict": "REJECT", "causality": {"passed": True},
                "walk_forward": {"oos_trades": 10, "oos_net_usd": -5, "passed": False}}

    monkeypatch.setattr(governed, "evaluate_ai_candidate", evaluate)
    result = governed.run_governed_ai_research(store, [ResearchTarget("delta_india", "BTC/USD:USD")],
                                              strategy_dir=strategy_dir, out_dir=out)
    row = result["candidates"][0]
    assert row["falsification"]["status"] == "CHALLENGED"
    assert row["performance_eligible"] is False
    assert "funding_costs" in row["falsification"]["unverified"]
    assert len(list((out / "attempts").glob("*.result.json"))) == 1
    saved = pd.read_parquet(out / "inputs" / f'{row["dataset_sha256"]}.parquet')
    pd.testing.assert_frame_equal(saved, bars())


@pytest.mark.parametrize("missing", ["data", "contract", "source", "universe"])
def test_missing_proof_never_calls_evaluator(tmp_path, monkeypatch, missing):
    store, strategy_dir, out = setup_run(tmp_path, bars() if missing != "data" else pd.DataFrame())
    if missing == "contract":
        next(strategy_dir.glob("*.experiment.json")).unlink()
    if missing == "source":
        with next(strategy_dir.glob("*.py")).open("a") as handle:
            handle.write("\n# drift\n")
    monkeypatch.setattr(governed, "evaluate_ai_candidate", lambda *a, **k: pytest.fail("must not test"))
    targets = [] if missing == "universe" else [ResearchTarget("delta_india", "BTC/USD:USD")]
    result = governed.run_governed_ai_research(store, targets, strategy_dir=strategy_dir, out_dir=out)
    assert result["candidates"][0]["verdict"] == "NOT_TESTABLE"
    assert result["candidates"][0]["walk_forward"] is None


def test_immutable_storage_refuses_rewrite(tmp_path):
    path = tmp_path / "packet.json"
    persist_once(path, b"first")
    persist_once(path, b"first")
    with pytest.raises(ValueError, match="immutable_artifact_conflict"):
        persist_once(path, b"other")
    assert path.read_bytes() == b"first"


def test_falsifier_does_not_turn_zero_or_positive_backtest_into_promotion():
    packet = {"packet_id": "a" * 64, "preflight": {"status": "READY_TO_TEST"}}
    for trades, expected in [(0, "INSUFFICIENT_EVIDENCE"), (50, "MORE_PROOF_REQUIRED")]:
        audit = falsify(packet, {"causality": {"passed": True},
                                 "walk_forward": {"oos_trades": trades, "oos_net_usd": 100, "passed": True}})
        assert audit["status"] == expected
        assert audit["can_promote"] is False
        assert "untouched_judgment" in audit["unverified"]


def test_real_pipeline_uses_governed_engine_and_daily_cache(tmp_path):
    from vnedge.research.continuous_ai_pipeline import run_continuous_ai_pipeline

    store, strategy_dir, out = setup_run(tmp_path, bars())
    target = [ResearchTarget("delta_india", "BTC/USD:USD")]
    first = run_continuous_ai_pipeline(store, target, strategy_dir=strategy_dir, out_dir=out)
    row = first["candidates"][0]
    assert row["preflight"]["status"] == "READY_TO_TEST"
    assert row["causality"]["passed"] is True
    assert row["walk_forward"]["oos_trades"] == 0
    assert row["falsification"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert row["can_promote"] is False
    assert first["governance"]["synthetic_fallback"] is False
    second = run_continuous_ai_pipeline(store, target, strategy_dir=strategy_dir, out_dir=out)
    assert second["evaluation_status"] == "CACHED"
    assert second["candidates"][0]["packet_id"] == row["packet_id"]


def test_worker_error_retains_started_and_failure_records(tmp_path, monkeypatch):
    store, strategy_dir, out = setup_run(tmp_path, bars())

    def crash(*args, **kwargs):
        raise RuntimeError("replay_failed")

    monkeypatch.setattr(governed, "evaluate_ai_candidate", crash)
    result = governed.run_governed_ai_research(
        store, [ResearchTarget("delta_india", "BTC/USD:USD")],
        strategy_dir=strategy_dir, out_dir=out)
    assert result["candidates"][0]["verdict"] == "ERROR"
    assert result["candidates"][0]["packet_id"]
    assert len(list((out / "attempts").glob("*.started.json"))) == 1
    assert len(list((out / "attempts").glob("*.result.json"))) == 1


def test_budget_rotation_does_not_starve_deferred_candidates(tmp_path, monkeypatch):
    store, strategy_dir, out = setup_run(tmp_path, pd.DataFrame())
    from vnedge.research.continuous_ai_pipeline import BLUEPRINTS
    materialize_next_blueprint(strategy_dir, blueprints=BLUEPRINTS[1:])
    monkeypatch.setattr(governed, "MAX_CANDIDATES_PER_CYCLE", 1)
    targets = [ResearchTarget("delta_india", "BTC/USD:USD")]
    first = governed.run_governed_ai_research(store, targets, strategy_dir=strategy_dir, out_dir=out)
    assert sum(c["verdict"] == "DEFERRED_BUDGET" for c in first["candidates"]) == 1
    next_offset = first["governance"]["next_candidate_offset"]
    second = governed.run_governed_ai_research(store, targets, strategy_dir=strategy_dir,
                                              out_dir=out, candidate_offset=next_offset)
    assert first["candidates"][0]["strategy_id"] != second["candidates"][0]["strategy_id"]


def test_runtime_fingerprint_works_without_git_in_container(monkeypatch):
    from vnedge.research import experiment_packet

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(experiment_packet.subprocess, "run", no_git)
    result = experiment_packet.runtime_identity()
    assert len(result["code_sha256"]) == 64
    assert result["python"]
    assert result["git_commit"]  # BUILD_SHA when present, otherwise explicit unavailable
