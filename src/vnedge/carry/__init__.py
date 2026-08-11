"""Cross-sectional factor engine (research infrastructure).

Market-neutral, cost-aware, strictly causal. This is a RESEARCH tool for
disciplined cross-sectional factor exploration — it does not trade and nothing
here is a validated edge. The first construction (`xsect_carry_v1`) FAILED its
sealed-tail test (net Sharpe +0.80 seen → +0.01 out-of-sample); see
research/prereg/xsect_carry_v1_20260811.md. The engine exists so future
constructions can be tested with the same seen/sealed discipline, never by
re-fishing a spent tail.
"""
from vnedge.carry.factor import CarryConfig, factor_pnl, funding_score, market_neutral_book
from vnedge.carry.backtest import FactorStats, evaluate, run_factor
from vnedge.carry.universe import CARRY_UNIVERSE, load_universe

__all__ = [
    "CARRY_UNIVERSE", "load_universe",
    "CarryConfig", "funding_score", "market_neutral_book", "factor_pnl",
    "FactorStats", "evaluate", "run_factor",
]
