from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from vnedge.research import continuous_ai_pipeline as pipeline
from vnedge.research.universe import ResearchTarget


def test_generator_creates_one_sandboxed_immutable_candidate_per_cycle(tmp_path):
    first = pipeline.materialize_next_blueprint(tmp_path)
    assert first is not None and first["status"] == "CREATED"
    assert first["can_trade"] is False and first["can_promote"] is False
    assert len(list(tmp_path.glob("*.py"))) == 1

    second = pipeline.materialize_next_blueprint(tmp_path)
    assert second is not None and second["status"] == "CREATED"
    assert len(list(tmp_path.glob("*.py"))) == 2

    # Existing source is immutable. A collision is visible and not overwritten.
    first_path = tmp_path / pipeline.BLUEPRINTS[0].filename
    first_path.write_text("# drifted")
    before = first_path.read_text()
    conflict = pipeline.materialize_next_blueprint(tmp_path)
    assert conflict is not None and conflict["status"] == "CONFLICT"
    assert first_path.read_text() == before


def test_pipeline_primary_target_order_prefers_delta_india_btc():
    ordered = pipeline._ordered_targets(
        (
            ResearchTarget("binanceusdm", "BTC/USDT:USDT", "1h"),
            ResearchTarget("delta_india", "ETH/USD:USD", "1h"),
            ResearchTarget("delta_india", "BTC/USD:USD", "1h"),
        )
    )
    assert ordered[0] == ResearchTarget("delta_india", "BTC/USD:USD", "1h")


def test_pipeline_evaluates_collects_evidence_then_uses_daily_cache(tmp_path, monkeypatch):
    strategy_dir = tmp_path / "strategies"
    out = tmp_path / "research"
    now = datetime(2026, 9, 8, tzinfo=UTC)
    source = pipeline.materialize_next_blueprint(strategy_dir)
    assert source is not None

    calls = 0

    def fake_build(store, targets, *, strategy_dir, experiment_dir, candidate_offset):
        assert experiment_dir == out / "experiments"
        nonlocal calls
        calls += 1
        filename = next(strategy_dir.glob("*.py")).name
        return {
            "generated_at": now.isoformat(),
            "dataset": {
                "exchange": "delta_india",
                "symbol": "BTC/USD:USD",
                "timeframe": "1h",
                "bars": 8_000,
                "source": "canonical_tick_lake",
            },
            "candidates": [
                {
                    "strategy_id": "ai_arena_ema_pullback_21_100_v1",
                    "source_file": filename,
                    "verdict": "REJECT",
                    "reasons": ["profit factor below gate"],
                    "causality": {"passed": True, "fired_bars": 14},
                    "walk_forward": {
                        "windows": 6,
                        "oos_trades": 14,
                        "oos_net_usd": -12.5,
                        "passed": False,
                    },
                    "can_trade": False,
                    "can_promote": False,
                }
            ],
            "rejected_files": [],
        }

    monkeypatch.setattr(pipeline, "build_ai_candidates_payload", fake_build)
    (out / "ml_pipeline_status.json").parent.mkdir(parents=True)
    (out / "ml_pipeline_status.json").write_text(
        json.dumps({"stage": "COLLECTING_LABELS", "dataset": {"samples": 12, "min_to_train": 200}})
    )

    first = pipeline.run_continuous_ai_pipeline(
        object(), (), strategy_dir=strategy_dir, out_dir=out,
        force_evaluate=True, now=now,
    )
    assert first["evaluation_status"] == "EVALUATED"
    assert first["summary"]["evaluated_candidates"] == 1
    assert first["summary"]["causal_candidates"] == 1
    assert first["ml"] == {
        "stage": "COLLECTING_LABELS", "samples": 12, "min_to_train": 200,
        "binding": False, "can_trade": False,
    }
    assert first["can_trade"] is False and first["can_promote"] is False
    assert first["candidates"][0]["evidence_id"]
    assert list((out / "ai_pipeline_evidence").glob("*.json"))
    assert len((out / "continuous_ai_pipeline_feed.jsonl").read_text().splitlines()) == 1

    cached = pipeline.run_continuous_ai_pipeline(
        object(), (), strategy_dir=strategy_dir, out_dir=out,
        now=now + timedelta(hours=1), retest_seconds=86_400,
    )
    assert cached["evaluation_status"] == "CACHED"
    assert calls == 1
    assert len((out / "continuous_ai_pipeline_feed.jsonl").read_text().splitlines()) == 1

    # Externally authored changes cannot inherit cached evidence from old bytes.
    contract = next(strategy_dir.glob("*.experiment.json"))
    contract.write_text(contract.read_text() + "\n")
    changed = pipeline.run_continuous_ai_pipeline(
        object(), (), strategy_dir=strategy_dir, out_dir=out,
        now=now + timedelta(hours=2), retest_seconds=86_400,
    )
    assert changed["evaluation_status"] == "EVALUATED"
    assert calls == 2
    (out / "ai_candidates.json").write_text("{}")
    repaired = pipeline.run_continuous_ai_pipeline(
        object(), (), strategy_dir=strategy_dir, out_dir=out,
        now=now + timedelta(hours=3),
    )
    assert repaired["evaluation_status"] == "EVALUATED"
    assert calls == 3


def test_pipeline_never_exposes_registration_roster_or_capital_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "build_ai_candidates_payload",
        lambda *args, **kwargs: {"dataset": {}, "candidates": [], "rejected_files": []},
    )
    payload = pipeline.run_continuous_ai_pipeline(
        object(), (), strategy_dir=tmp_path / "strategies", out_dir=tmp_path / "out",
        force_evaluate=True,
    )
    assert payload["policy"]["auto_register"] is False
    assert payload["policy"]["roster_mutation"] is False
    assert payload["policy"]["capital_mutation"] is False
    assert payload["live_orders_enabled"] is False
