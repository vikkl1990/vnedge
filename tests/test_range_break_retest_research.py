from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pandas as pd
import pytest

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.plan.cost_model import CostModel
from vnedge.research.range_break_retest import SPEC, RangeBreakRetestResearch
from vnedge.strategy.scanner_contracts import scanner_runtime_contract


def seal(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    hashes = []
    for row in frame.to_dict("records"):
        ts = pd.Timestamp(row["timestamp"]).to_pydatetime()
        hashes.append(bar_content_sha256(row, open_time=ts,
                      close_time=ts + pd.Timedelta(minutes=5), source=row["candle_source"]))
    frame["content_sha256"] = hashes
    return frame


def fixture(*, short: bool = False, scale: float = 1.0, symbol: str = "BTCUSD") -> pd.DataFrame:
    rows = [{"open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0}
            for _ in range(20)]
    rows += [{"open": 101.7, "high": 102.4, "low": 101.7, "close": 102.3},
             {"open": 102.05, "high": 102.5, "low": 102.0, "close": 102.4}]
    for i, row in enumerate(rows):
        if short:
            row = {"open": 200 - row["open"], "high": 200 - row["low"],
                   "low": 200 - row["high"], "close": 200 - row["close"]}
        rows[i] = {k: 100 + scale * (v - 100) for k, v in row.items()}
        rows[i].update(timestamp=pd.Timestamp("2026-09-12T00:00:00Z") + pd.Timedelta(minutes=5*i),
                       symbol=symbol, exchange="delta_india", timeframe="5m", volume=5.0,
                       candle_source="canonical_tick_lake", is_closed=True,
                       data_quality="ok", coverage_ok=True)
    return seal(pd.DataFrame(rows))


@pytest.mark.parametrize("short", [False, True])
def test_valid_setup_retains_structural_geometry_and_bound_evidence(short: bool) -> None:
    engine, frame = RangeBreakRetestResearch("BTCUSD"), fixture(short=short)
    result = engine.evaluate(frame, 21)
    assert not result.failed_gates
    candidate = result.candidate
    assert candidate is not None
    assert candidate.signal.side == ("short" if short else "long")
    assert candidate.signal.stop_price == pytest.approx(98.2 if short else 101.8)
    assert candidate.signal.take_profit_price == pytest.approx(94 if short else 106)
    assert candidate.signal.expected_gross_edge_bps is None
    assert candidate.signal.edge_model_id is None
    assert candidate.signal.decision_envelope is not None
    assert candidate.signal.permission_snapshot.context_bars == ()
    assert candidate.signal.decision_envelope.entry_clock == "next_5m_open"
    assert len(candidate.input_bar_hashes) == 22
    assert 0 <= candidate.quality_score <= 100
    assert candidate.booked_round_bps == pytest.approx(17.8)
    assert candidate.path_id == "research_observe"
    assert not candidate.can_trade and not candidate.can_promote and not candidate.performance_eligible
    with pytest.raises(FrozenInstanceError):
        candidate.can_trade = True


def test_analyze_keeps_list_interface_without_external_checkout_or_htf() -> None:
    frame = fixture()
    engine = RangeBreakRetestResearch("BTCUSD")
    assert engine.get_required_timeframes() == ["5m"]
    assert engine.analyze("BTCUSD", {"5m": frame}) == [engine.signal(frame, 21)]
    assert engine.analyze("BTCUSD", {}) == []
    assert engine.evaluate_market("BTCUSD", {}).failed_gates == ("decision_frame_missing",)
    with pytest.raises(ValueError, match="symbol"):
        engine.analyze("ETHUSD", {"5m": frame})
    with pytest.raises(ValueError, match="universe"):
        RangeBreakRetestResearch("SOLUSD")


def test_no_mutation_prefix_parity_restart_and_no_repeat_on_next_bar() -> None:
    engine, frame = RangeBreakRetestResearch("BTCUSD"), fixture()
    original = frame.copy(deep=True)
    expected = engine.evaluate(engine.prepare(frame), 21)
    future = frame.iloc[[-1]].copy()
    future["timestamp"] += pd.Timedelta(minutes=5)
    future[["open", "high", "low", "close"]] = [500.0, 600.0, 400.0, 550.0]
    extended = seal(pd.concat([frame, future], ignore_index=True))
    assert engine.evaluate(engine.prepare(extended), 21) == expected
    assert RangeBreakRetestResearch("BTCUSD").evaluate(frame, 21) == expected
    assert engine.evaluate(extended, 22).candidate is None
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize(("column", "value", "gate"), [
    ("is_closed", False, "decision_row_not_closed"),
    ("data_quality", "gap", "decision_row_quality_not_ok"),
    ("coverage_ok", False, "coverage_not_ok"),
    ("coverage_ok", pd.NA, "coverage_not_ok"),
    ("coverage_ok", "true", "coverage_not_ok"),
    ("volume", 0.0, "positive_base_volume_required"),
    ("volume", float("nan"), "positive_base_volume_required"),
    ("symbol", "ETHUSD", "symbol_identity_mismatch"),
    ("symbol", pd.NA, "symbol_identity_mismatch"),
    ("timeframe", "15m", "timeframe_identity_mismatch"),
    ("exchange", "binanceusdm", "exchange_identity_mismatch"),
    ("content_sha256", "0" * 64, "decision_row_content_hash_mismatch"),
    ("content_sha256", "", "decision_row_content_hash_missing"),
])
def test_every_consumed_bar_is_validated(column: str, value: object, gate: str) -> None:
    frame = fixture()
    frame[column] = frame[column].astype(object)
    frame.at[4, column] = value
    result = RangeBreakRetestResearch("BTCUSD").evaluate(frame, 21)
    assert result.candidate is None
    assert gate in result.failed_gates


@pytest.mark.parametrize("duplicate", [False, True])
def test_rehashed_gap_or_duplicate_cannot_bridge_structure(duplicate: bool) -> None:
    frame = fixture()
    if duplicate:
        frame.loc[10, "timestamp"] = frame.loc[9, "timestamp"]
    else:
        frame.loc[10:, "timestamp"] += pd.Timedelta(minutes=5)
    result = RangeBreakRetestResearch("BTCUSD").evaluate(seal(frame), 21)
    assert "decision_window_gap_or_duplicate" in result.failed_gates


def test_official_candle_rejected_even_with_valid_hash() -> None:
    frame = fixture()
    frame["candle_source"] = "official_delta_ohlc"
    result = RangeBreakRetestResearch("BTCUSD").evaluate(seal(frame), 21)
    assert result.candidate is None
    assert any("source" in reason for reason in result.failed_gates)


def test_cost_and_geometry_failures_are_both_visible_not_score_overrides() -> None:
    result = RangeBreakRetestResearch("BTCUSD").evaluate(fixture(scale=0.01), 21)
    assert result.candidate is None
    assert "target_room_below_cost_floor" in result.failed_gates
    assert "net_reward_risk_too_small" in result.failed_gates
    assert result.score_components
    diagnostics = result.diagnostics()
    assert diagnostics["score_kind"] == "geometry_rank_not_probability"
    assert diagnostics["expected_gross_edge_bps"] is None
    assert "oos_edge_unproven" in diagnostics["execution_blockers"]


def test_cost_profile_drift_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    cost = CostModel.for_profile(SPEC.cost_profile_id)
    changed = CostModel(replace(cost.config, taker_fee_bps=1.0))
    monkeypatch.setattr(CostModel, "for_profile", classmethod(lambda cls, profile: changed))
    assert RangeBreakRetestResearch("BTCUSD").evaluate(fixture(), 21).failed_gates == ("cost_contract_changed",)


def test_retest_cannot_be_a_far_away_candle_or_reclaim_inside_range() -> None:
    engine = RangeBreakRetestResearch("BTCUSD")
    far = fixture()
    far.loc[21, ["open", "high", "low", "close"]] = [103.0, 103.6, 103.0, 103.5]
    result = engine.evaluate(seal(far), 21)
    assert "boundary_retest_missing" in result.failed_gates
    assert "retest_entry_overextended" in result.failed_gates
    failed = fixture()
    failed.loc[21, ["open", "high", "low", "close"]] = [102.1, 102.2, 101.9, 101.95]
    assert "retest_hold_failed" in engine.evaluate(seal(failed), 21).failed_gates


def test_evidence_binds_entire_input_window_not_just_final_row() -> None:
    engine, frame = RangeBreakRetestResearch("BTCUSD"), fixture()
    first = engine.evaluate(frame, 21).candidate
    frame.loc[3, "volume"] = 10
    second = engine.evaluate(seal(frame), 21).candidate
    assert first.episode_id == second.episode_id
    assert first.evidence_id != second.evidence_id
    assert first.signal.decision_envelope.decision_id != second.signal.decision_envelope.decision_id


def test_warmup_and_unregistered_status_are_explicit() -> None:
    engine, frame = RangeBreakRetestResearch("BTCUSD"), fixture()
    assert engine.evaluate(frame, 20).failed_gates == ("warmup_incomplete",)
    with pytest.raises(IndexError):
        engine.evaluate(frame, -1)
    assert scanner_runtime_contract(SPEC.strategy_id) is None


def test_symbols_never_share_decision_or_episode_ids() -> None:
    btc = RangeBreakRetestResearch("BTCUSD").evaluate(fixture(), 21).candidate
    eth = RangeBreakRetestResearch("ETHUSD").evaluate(fixture(symbol="ETHUSD"), 21).candidate
    assert btc.episode_id != eth.episode_id
    assert btc.signal.decision_envelope.decision_id != eth.signal.decision_envelope.decision_id


def test_foreign_feature_columns_cannot_rewrite_permission_or_decision_id() -> None:
    engine, frame = RangeBreakRetestResearch("BTCUSD"), fixture()
    first = engine.evaluate(frame, 21).candidate
    frame["mreg_state"] = "flat"
    frame["mreg_reason"] = "some_other_scanner"
    frame["bos15_htf"] = "down"
    second = engine.evaluate(frame, 21).candidate
    assert first == second
    assert second.signal.permission_snapshot.regime_state == "not_applicable"


def test_duplicate_columns_fail_before_feature_computation() -> None:
    frame = fixture()
    ambiguous = pd.concat([frame, frame[["close"]]], axis=1)
    assert RangeBreakRetestResearch("BTCUSD").evaluate(ambiguous, 21).failed_gates == ("duplicate_input_columns",)


def test_finite_inputs_cannot_emit_overflowed_target() -> None:
    frame = fixture()
    frame[["open", "high", "low", "close"]] *= 1.7e306
    result = RangeBreakRetestResearch("BTCUSD").evaluate(seal(frame), 21)
    assert result.candidate is None
    assert "nonfinite_geometry" in result.failed_gates


@pytest.mark.parametrize("mutation", ["forming_retest", "weak_break", "overextended_break", "naive_time"])
def test_additional_closed_bar_and_pattern_boundaries(mutation: str) -> None:
    frame = fixture()
    if mutation == "forming_retest":
        frame.loc[21, "is_closed"] = False
        expected = "decision_row_not_closed"
    elif mutation == "weak_break":
        frame.loc[20, "high"] = 104.0
        expected = "breakout_body_too_small"
    elif mutation == "overextended_break":
        frame.loc[20, ["high", "close"]] = [103.3, 103.2]
        expected = "breakout_overextended"
    else:
        frame["timestamp"] = frame.timestamp.astype(object)
        frame.loc[21, "timestamp"] = frame.loc[21, "timestamp"].tz_localize(None)
        expected = "utc_timestamp_required"
    result = RangeBreakRetestResearch("BTCUSD").evaluate(seal(frame), 21)
    assert result.candidate is None
    assert expected in result.failed_gates
