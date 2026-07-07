"""Alpha distillation pack: public concepts become causal VNEDGE atoms."""

import pandas as pd

from vnedge.data.schemas import normalize_candles
from vnedge.research.alpha_distillation import (
    AlphaDistillationCandidate,
    parse_candidate,
    run_alpha_distillation_research,
)
from vnedge.strategy.alpha_distillation_pack import (
    FEATURE_ATOMS,
    AlphaDistillationPack,
    add_alpha_distillation_columns,
    concept_coverage,
    concept_inventory,
    default_alpha_distillation_params,
)
from vnedge.strategy.strategy_registry import STRATEGIES


BASE = 1_750_000_000_000
FIFTEEN = 15 * 60_000


def make_candles(rows):
    return normalize_candles(
        [
            [BASE + i * FIFTEEN, open_, high, low, close, volume]
            for i, (open_, high, low, close, volume) in enumerate(rows)
        ]
    )


def trend_rows(n=180, start=100.0, step=0.04, volume=100.0):
    rows = []
    prev = start
    for i in range(n):
        close = start + i * step
        high = max(prev, close) + 0.10
        low = min(prev, close) - 0.10
        rows.append((prev, high, low, close, volume))
        prev = close
    return rows


def signal_row(**overrides):
    row = {
        "timestamp": pd.Timestamp("2026-07-06T00:00:00Z"),
        "open": 99.4,
        "high": 101.0,
        "low": 98.8,
        "close": 100.0,
        "volume": 1000.0,
        "atr": 1.0,
        "prior_low": 99.0,
        "prior_high": 103.0,
        "volatility_ok": True,
        "long_distilled_score": 10.0,
        "short_distilled_score": 2.0,
        "long_expected_edge_bps": 10.5,
        "short_expected_edge_bps": 0.0,
        "long_exit_quality": 86.0,
        "short_exit_quality": 20.0,
        "long_context_score": 2.0,
        "short_context_score": -2.0,
        "trigger_1m_long": True,
        "trigger_1m_short": False,
        "long_primary_atom": "liquidity_sweep",
        "short_primary_atom": "confluence",
        "long_route": "MAKER_FIRST_RESEARCH",
        "short_route": "BLOCKED_FEE_WALL",
        "sweep_low": True,
        "bullish_fvg_retest": True,
        "bull_order_block_proxy": False,
        "squeeze_release_up": False,
        "vwap_reclaim_long": False,
        "bos_up": True,
        "trend_trail_long": True,
        "profile_reclaim_long": False,
        "momentum_impulse_long": True,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_concept_inventory_covers_35_public_ideas_without_copying_scripts():
    inventory = concept_inventory()
    coverage = concept_coverage()

    assert len(inventory) == 35
    assert set(coverage).issuperset(FEATURE_ATOMS)
    assert "Liquidity Trail Matrix" in coverage["trend_trail"]
    assert "FVG Retest Engine / SMC Strategy" in coverage["fvg_retest"]
    assert "Squeeze Breakout Pro" in coverage["squeeze_release"]
    assert STRATEGIES["alpha_distillation_pack_v1"] is AlphaDistillationPack


def test_alpha_distillation_features_are_causal_when_future_changes():
    candles = make_candles(trend_rows())
    mutated = candles.copy()
    mutated.loc[100:, ["open", "high", "low", "close"]] *= 1.25
    params = default_alpha_distillation_params()

    a = add_alpha_distillation_columns(candles, params)
    b = add_alpha_distillation_columns(mutated, params)

    cols = [
        "trend_trail_long",
        "profile_reclaim_long",
        "momentum_impulse_long",
        "liquidity_cluster_long",
        "long_distilled_score",
        "long_expected_edge_bps",
        "long_exit_quality",
        "long_primary_atom",
    ]
    pd.testing.assert_frame_equal(a.loc[:90, cols], b.loc[:90, cols])


def test_alpha_distillation_emits_only_after_fee_and_exit_gates():
    strategy = AlphaDistillationPack(
        min_score=8.0,
        min_edge_bps=9.0,
        require_context=True,
        require_1m_trigger=True,
    )

    intent = strategy.signal(signal_row(), 0)
    blocked = strategy.signal(signal_row(long_expected_edge_bps=7.5), 0)

    assert intent is not None
    assert intent.side == "long"
    assert intent.stop_price < 100.0
    assert intent.take_profit_price > 100.0
    assert "alpha_distillation_pack long liquidity_sweep" in intent.reason
    assert "route=MAKER_FIRST_RESEARCH" in intent.reason
    assert blocked is None


def test_alpha_distillation_respects_atom_and_side_filters():
    long_only = AlphaDistillationPack(allowed_atoms=("fvg_retest",), min_score=8.0)
    short_only = AlphaDistillationPack(allowed_sides=("short",), min_score=8.0)

    assert long_only.signal(signal_row(long_primary_atom="liquidity_sweep"), 0) is None
    assert short_only.signal(signal_row(), 0) is None


def test_alpha_distillation_research_marks_missing_lanes_untestable(tmp_path):
    candidate = AlphaDistillationCandidate("binanceusdm", "DOGE/USDT:USDT")

    report = run_alpha_distillation_research(tmp_path, candidates=(candidate,))

    result = report["results"][0]
    assert result["verdict"] == "UNTESTABLE"
    assert "missing data lane" in result["reasons"][0]
    assert report["policy"]["can_trade"] is False
    assert report["policy"]["concept_count"] == 35
    assert report["summary"]["untestable"] == 1


def test_parse_candidate_accepts_atom_and_side_filters():
    candidate = parse_candidate("bybit|SOL/USDT:USDT|fvg_retest,squeeze_release|long")

    assert candidate.exchange == "bybit"
    assert candidate.symbol == "SOL/USDT:USDT"
    assert candidate.allowed_atoms == ("fvg_retest", "squeeze_release")
    assert candidate.allowed_sides == ("long",)
