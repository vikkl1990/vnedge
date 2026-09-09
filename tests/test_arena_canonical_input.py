from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.research.canonical_input import CanonicalResearchStore
from vnedge.research.continuous_ai_pipeline import materialize_next_blueprint, run_continuous_ai_pipeline
from vnedge.research.experiment_contract import bind_contract
from vnedge.research.experiment_packet import preflight, ExperimentSpec
from vnedge.research.universe import ResearchTarget


def lake(tmp_path, n=130):
    writer = CandleParquetStore(tmp_path / "candles", exchange="delta_india")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [Candle(symbol="BTCUSD", timeframe="1h", open_time=start + timedelta(hours=i),
                      close_time=start + timedelta(hours=i+1), open=Decimal("100"),
                      high=Decimal("102"), low=Decimal("98"), close=Decimal("101"),
                      volume=Decimal("0.123456789012345678"),
                      quote_volume=Decimal("12.469135690246913478"), trade_count=1,
                      taker_buy_volume=Decimal("0"), vwap=Decimal("101")) for i in range(n)]
    writer.upsert(candles)
    return CanonicalResearchStore(writer.root), writer.partition_path(candles[0])


def setup(tmp_path):
    strategy_dir = tmp_path / "strategies"
    materialize_next_blueprint(strategy_dir)
    path = next(strategy_dir.glob("*.experiment.json"))
    spec = json.loads(path.read_text())
    spec.update(train_bars=110, test_bars=10)
    path.write_text(json.dumps(spec))
    return strategy_dir, ExperimentSpec.model_validate(spec)


def validate(store, spec):
    frame = store.read_candles("delta_india", "BTC/USD:USD", "1h")
    return preflight(spec, frame, warmup_bars=103, now=datetime(2026, 9, 9, tzinfo=UTC),
                     source_sha256=spec.source_sha256, strategy_id=spec.strategy_id)


def test_recorder_to_arena_to_real_evaluator(tmp_path):
    store, partition = lake(tmp_path)
    strategy_dir, spec = setup(tmp_path)
    original = partition.read_bytes()
    assert validate(store, spec)["status"] == "READY_TO_TEST"
    result = run_continuous_ai_pipeline(store, [ResearchTarget("delta_india", "BTC/USD:USD")],
                                        strategy_dir=strategy_dir, out_dir=tmp_path / "out")
    row = result["candidates"][0]
    assert row["causality"]["passed"]
    assert row["walk_forward"] is not None
    assert row["packet_id"] and row["dataset_sha256"]
    assert result["summary"]["backtested_candidates"] == 1
    assert not row["can_promote"] and not row["can_trade"]
    assert partition.read_bytes() == original


@pytest.mark.parametrize("column", ["source", "is_closed", "coverage_ok", "content_sha256"])
def test_no_legacy_provenance_upgrade(tmp_path, column):
    store, path = lake(tmp_path)
    pd.read_parquet(path).drop(columns=column).to_parquet(path)
    with pytest.raises(ValueError, match="canonical_persisted_proof_missing"):
        store.read_candles("delta_india", "BTC/USD:USD", "1h")


@pytest.mark.parametrize("kind,reason", [
    ("hash", "decision_row_content_hash_mismatch"),
    ("gap", "non_consecutive_bars"), ("duplicate", "non_consecutive_bars"),
    ("forming", "decision_row_not_closed"), ("string_flag", "decision_row_not_closed"),
    ("coverage", "coverage_unproven"), ("official", "untrusted permission candle source: official_delta_ohlc"),
])
def test_bad_lake_rows_stay_rejected(tmp_path, kind, reason):
    store, path = lake(tmp_path)
    _, spec = setup(tmp_path)
    frame = pd.read_parquet(path)
    if kind == "hash": frame.loc[0, "content_sha256"] = "a" * 64
    elif kind == "gap": frame = frame.drop(index=10)
    elif kind == "duplicate": frame = pd.concat([frame, frame.tail(1)], ignore_index=True)
    elif kind == "forming": frame.loc[0, "is_closed"] = False
    elif kind == "string_flag": frame["is_closed"] = "false"
    elif kind == "coverage": frame.loc[0, "coverage_ok"] = False
    elif kind == "official": frame["source"] = "official_delta_ohlc"
    frame.to_parquet(path)
    check = validate(store, spec)
    assert check["status"] == "NOT_TESTABLE"
    assert reason in check["failures"]
    if kind in {"gap", "duplicate"}:
        assert check["invalid_row_counts"]["non_consecutive_bars"] == 1


def test_partition_identity_and_working_budget(tmp_path):
    store, path = lake(tmp_path)
    bounded = CanonicalResearchStore(store.root, max_bars=10)
    assert len(bounded.read_candles("delta_india", "BTC/USD:USD", "1h")) == 10
    frame = pd.read_parquet(path)
    frame["symbol"] = "ETHUSD"
    frame.to_parquet(path)
    with pytest.raises(ValueError, match="canonical_partition_symbol_mismatch"):
        store.read_candles("delta_india", "BTC/USD:USD", "1h")
    with pytest.raises(ValueError, match="bar_budget_invalid"):
        CanonicalResearchStore(store.root, max_bars=0)


def test_missing_delta_does_not_read_generic_or_other_venue(tmp_path):
    store = CanonicalResearchStore(tmp_path)
    with pytest.raises(ValueError, match="canonical_history_missing"):
        store.read_candles("delta_india", "BTC/USD:USD", "1h")
    with pytest.raises(ValueError, match="product_unsupported"):
        store.read_candles("binanceusdm", "BTC/USD:USD", "1h")


def test_explicit_contract_binding_refuses_source_drift_and_replacement(tmp_path):
    strategy_dir, spec = setup(tmp_path)
    source = next(strategy_dir.glob("*.py"))
    sidecar = next(strategy_dir.glob("*.experiment.json"))
    sidecar.unlink()
    draft = tmp_path / "spec.json"
    draft.write_text(spec.model_dump_json())
    assert bind_contract(source, draft) == sidecar
    before = sidecar.read_bytes()
    draft.write_text(spec.model_copy(update={"claim": "A different explicit market claim."}).model_dump_json())
    with pytest.raises(ValueError, match="immutable_artifact_conflict"):
        bind_contract(source, draft)
    assert sidecar.read_bytes() == before
    source.write_text(source.read_text() + "\n# changed\n")
    with pytest.raises(ValueError, match="candidate_identity_mismatch"):
        bind_contract(source, draft)
