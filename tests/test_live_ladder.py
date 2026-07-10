"""Live promotion ladder — explainable, no skipping, no automatic live."""

from vnedge.config.settings import LIVE_CONFIRMATION_PHRASE, Settings, TradingMode
from vnedge.runtime.live_ladder import (
    LiveLadderEvidence,
    LiveLadderStage,
    evaluate_live_ladder,
    settings_live_gates_ready,
)


def test_backtest_to_paper_requires_locked_untouched_human_evidence():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.BACKTEST,
            target_stage=LiveLadderStage.PAPER,
            params_locked=True,
            model_registered=True,
            untouched_judgment_passed=True,
            human_approved=True,
        )
    )

    assert decision.allowed
    assert decision.blockers == ()


def test_backtest_to_paper_lists_all_missing_evidence():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.BACKTEST,
            target_stage=LiveLadderStage.PAPER,
        )
    )

    assert not decision.allowed
    assert set(decision.blockers) == {
        "paper requires frozen, versioned strategy parameters",
        "paper requires a strategy/model registry entry",
        "paper requires a passed untouched-data judgment",
        "paper requires explicit human approval",
    }


def test_ladder_cannot_skip_from_paper_to_live_small():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.PAPER,
            target_stage=LiveLadderStage.LIVE_SMALL,
            human_approved=True,
            pre_live_checklist_cleared=True,
            three_live_gates_ready=True,
            shadow_days=30,
            shadow_trades=50,
            shadow_net_usd=25,
            shadow_profit_factor=1.4,
        )
    )

    assert not decision.allowed
    assert any("must advance exactly one rung" in b for b in decision.blockers)


def test_paper_to_shadow_requires_positive_mature_paper_trial():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.PAPER,
            target_stage=LiveLadderStage.SHADOW,
            human_approved=True,
            paper_days=14,
            paper_trades=10,
            paper_net_usd=1.0,
            paper_max_drawdown_pct=6.0,
        )
    )

    assert decision.allowed


def test_shadow_to_live_small_requires_live_safety_and_shadow_edge():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.SHADOW,
            target_stage=LiveLadderStage.LIVE_SMALL,
            human_approved=True,
            pre_live_checklist_cleared=True,
            three_live_gates_ready=True,
            reconciliation_clean=True,
            kill_switch_clear=True,
            journal_writable=True,
            shadow_days=7,
            shadow_trades=10,
            shadow_net_usd=5.0,
            shadow_profit_factor=1.05,
            shadow_max_drawdown_pct=6.0,
        )
    )

    assert decision.allowed


def test_shadow_to_live_small_blocks_without_gates_or_profit_factor():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.SHADOW,
            target_stage=LiveLadderStage.LIVE_SMALL,
            human_approved=True,
            pre_live_checklist_cleared=True,
            shadow_days=7,
            shadow_trades=10,
            shadow_net_usd=5.0,
        )
    )

    assert not decision.allowed
    assert "three live gates are not open" in decision.blockers
    assert "shadow trial profit factor is missing" in decision.blockers


def test_live_small_to_live_full_requires_positive_live_small_observation():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.LIVE_SMALL,
            target_stage=LiveLadderStage.LIVE_FULL,
            human_approved=True,
            pre_live_checklist_cleared=True,
            three_live_gates_ready=True,
            live_small_days=7,
            live_small_trades=5,
            live_small_net_usd=2.5,
            live_small_max_drawdown_pct=3.0,
        )
    )

    assert decision.allowed


def test_live_full_is_terminal():
    decision = evaluate_live_ladder(
        LiveLadderEvidence(
            current_stage=LiveLadderStage.LIVE_FULL,
            target_stage=LiveLadderStage.LIVE_FULL,
        )
    )

    assert not decision.allowed
    assert "live_full is terminal; no higher promotion rung exists" in decision.blockers


def test_settings_helper_uses_existing_three_gate_contract():
    live = Settings(
        _env_file=None,
        trading_mode=TradingMode.LIVE_SMALL,
        live_trading_enabled=True,
        confirm_live_trading=LIVE_CONFIRMATION_PHRASE,
    )
    shadow = Settings(
        _env_file=None,
        trading_mode=TradingMode.SHADOW,
        live_trading_enabled=True,
        confirm_live_trading=LIVE_CONFIRMATION_PHRASE,
    )

    assert settings_live_gates_ready(live)
    assert not settings_live_gates_ready(shadow)
