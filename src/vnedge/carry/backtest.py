"""Metrics + windowed runner for a cross-sectional factor P&L series."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from vnedge.carry.factor import CarryConfig, factor_pnl

_ANN = 365 ** 0.5


@dataclass(frozen=True)
class FactorStats:
    sharpe: float
    tstat: float
    total_return: float
    max_drawdown: float
    avg_turnover: float
    n: int


def evaluate(pnl: pd.Series, avg_turnover: float = 0.0) -> FactorStats:
    if len(pnl) < 2 or pnl.std() == 0:
        return FactorStats(0.0, 0.0, 0.0, 0.0, avg_turnover, len(pnl))
    mean, sd = pnl.mean(), pnl.std()
    curve = (1 + pnl).cumprod()
    dd = float((curve / curve.cummax() - 1).min())
    return FactorStats(
        sharpe=float(mean / sd * _ANN),
        tstat=float(mean / sd * len(pnl) ** 0.5),
        total_return=float(curve.iloc[-1] - 1.0),
        max_drawdown=dd,
        avg_turnover=avg_turnover,
        n=len(pnl),
    )


def run_factor(
    close: pd.DataFrame,
    funding: pd.DataFrame,
    cfg: CarryConfig,
    *,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> tuple[pd.Series, FactorStats]:
    """Full-history P&L, then stats over the [start, end] slice (OOS discipline:
    compute the book over all history but judge only the requested window)."""
    pnl = factor_pnl(close, funding, cfg)
    sliced = pnl
    if start is not None:
        sliced = sliced[sliced.index >= start]
    if end is not None:
        sliced = sliced[sliced.index <= end]
    return sliced, evaluate(sliced)
