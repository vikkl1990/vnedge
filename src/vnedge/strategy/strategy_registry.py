"""Strategy registry — the single place strategies are looked up by name.

Later milestones (approval workflow, live config) reference strategies by
registry name only, so an approved strategy is always a specific, importable
class — never an ad-hoc script.
"""

from __future__ import annotations

from vnedge.strategy.alpha_stack import AlphaStackConfluence
from vnedge.strategy.alpha_distillation_pack import AlphaDistillationPack
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.context_scalper_v2 import ContextScalperV2
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin
from vnedge.strategy.datrend_nomada_scalper import DATrendNomadaScalper
from vnedge.strategy.fvg_liquidity_breakout import FvgLiquidityBreakoutScanner
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.luxara_break_bounce_v27 import LuxaraBreakBounceV27Scanner
from vnedge.strategy.luxara_live_plan_qtm import LuxaraLivePlanQTMScanner
from vnedge.strategy.luxy_ut_bot_forecast import LuxyUTBotForecastScanner
from vnedge.strategy.momentum_cascade_lyro import MomentumCascadeLyroScanner
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.quant_signal_pack import QuantSignalPack
from vnedge.strategy.quantified_fee_wall_sniper import QuantifiedFeeWallSniper
from vnedge.strategy.sats_5m_scalper import Sats5mScalper
from vnedge.strategy.smc_playbook_scalper import SMCPlaybookScalper
from vnedge.strategy.stealth_trail_bbp import (
    HumanTradeFingerprintScanner,
    StealthTrailBBPScanner,
)
from vnedge.strategy.trend_continuation import TrendContinuation
from vnedge.strategy.trend_retest import TrendRetest
from vnedge.strategy.vnedge_algo_ml_pro import VNEDGEAlgoMLProScanner
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout

STRATEGIES: dict[str, type[BaseStrategy]] = {
    CryptoTrendAtrMargin.strategy_id: CryptoTrendAtrMargin,
    TrendContinuation.strategy_id: TrendContinuation,
    FundingMeanReversion.strategy_id: FundingMeanReversion,
    VolatilityExpansionBreakout.strategy_id: VolatilityExpansionBreakout,
    PanicReversal.strategy_id: PanicReversal,
    FundingSqueezeContinuation.strategy_id: FundingSqueezeContinuation,
    DATrendNomadaScalper.strategy_id: DATrendNomadaScalper,
    FvgLiquidityBreakoutScanner.strategy_id: FvgLiquidityBreakoutScanner,
    AlphaStackConfluence.strategy_id: AlphaStackConfluence,
    QuantSignalPack.strategy_id: QuantSignalPack,
    Sats5mScalper.strategy_id: Sats5mScalper,
    StealthTrailBBPScanner.strategy_id: StealthTrailBBPScanner,
    HumanTradeFingerprintScanner.strategy_id: HumanTradeFingerprintScanner,
    SMCPlaybookScalper.strategy_id: SMCPlaybookScalper,
    TrendRetest.strategy_id: TrendRetest,
    AlphaDistillationPack.strategy_id: AlphaDistillationPack,
    LuxyUTBotForecastScanner.strategy_id: LuxyUTBotForecastScanner,
    MomentumCascadeLyroScanner.strategy_id: MomentumCascadeLyroScanner,
    LuxaraLivePlanQTMScanner.strategy_id: LuxaraLivePlanQTMScanner,
    LuxaraBreakBounceV27Scanner.strategy_id: LuxaraBreakBounceV27Scanner,
    VNEDGEAlgoMLProScanner.strategy_id: VNEDGEAlgoMLProScanner,
    ContextScalperV2.strategy_id: ContextScalperV2,
    QuantifiedFeeWallSniper.strategy_id: QuantifiedFeeWallSniper,
}


# Structurally over-fit families — geometry (FVG / liquidity), Pine-port stacks
# (Luxara), and confluence votes (alpha_stack / quant_signal / momentum cascade /
# distillation). Their flaws are architectural (DOF explosion, geometry ≠
# mechanism, unitless confluence scores, private cost math), not bad luck OOS —
# see docs/SCANNER_REVIEW_20260813. They stay importable for RESEARCH and may run
# a SHADOW lane for observation, but must NEVER back a capital (paper/live) lane.
# The fix for a dead scanner is inertness + a locked re-registration under the
# TradePlan contract (G1-style), NEVER an in-place refactor of its zoo of knobs.
# Derived from the class ids so the guard can never drift from a renamed strategy.
_RESEARCH_ONLY_CLASSES = (
    FvgLiquidityBreakoutScanner,
    LuxaraBreakBounceV27Scanner, LuxaraLivePlanQTMScanner, LuxyUTBotForecastScanner,
    AlphaStackConfluence, QuantSignalPack, MomentumCascadeLyroScanner,
    AlphaDistillationPack,
)
RESEARCH_ONLY: frozenset[str] = frozenset(c.strategy_id for c in _RESEARCH_ONLY_CLASSES)


# Post-mortem KILLS — strategies whose edge was investigated through the promotion
# machinery and FAILED (forward-paper loss or IS/OOS collapse), as opposed to the
# RESEARCH_ONLY families above whose flaw is structural over-fit by construction. A
# kill revokes capital permission exactly like RESEARCH_ONLY (may still run a SHADOW
# lane for continued measurement — "delete permission to trade, not the measurement
# code"). Re-enabling requires NEW structural evidence on untouched data through the
# ladder, never an in-place tweak. See docs/EDGE_INVESTIGATION_POSTMORTEM_20260816.
_KILLED_CLASSES = (
    # funding_mean_reversion_v1 — forward paper FAILED 2026-08-14: net -$16.60,
    # breached the 6% DD cap (7.35%), 3W/5L over 8 trades, despite 3x OOS-positive
    # backtests. Thin and fee-sensitive; dies at the ~8bps taker cost wall.
    FundingMeanReversion,
)
KILLED: frozenset[str] = frozenset(c.strategy_id for c in _KILLED_CLASSES)


def is_capital_eligible(strategy_id: str) -> bool:
    """False for research-only families AND post-mortem-killed strategies: both may
    run SHADOW (observation) but must never back a capital (paper/live) lane."""
    return strategy_id not in RESEARCH_ONLY and strategy_id not in KILLED


def get_strategy_class(strategy_id: str) -> type[BaseStrategy]:
    try:
        return STRATEGIES[strategy_id]
    except KeyError:
        raise KeyError(
            f"unknown strategy '{strategy_id}' — registered: {sorted(STRATEGIES)}"
        ) from None
