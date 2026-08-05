"""Portfolio backtester: combine independent single-symbol strategy backtests
into ONE risk-managed book, and measure the diversification benefit.

Each leg is an independent single-symbol ``run_backtest`` (risk-per-trade
sizing), reduced to a daily-PnL series. The portfolio is a capital-weighted sum
of the legs' daily PnL on a shared equity base, so weights model each leg's
SHARE of the book (they sum to 1). This answers the core question for turning
thin single edges into a survivable book: does running them together raise
risk-adjusted return and cut drawdown, and how correlated are they?

It does NOT model capital competition between legs bar-by-bar (fine for
risk-per-trade sizing, where each leg's PnL scales ~linearly with its share); a
bar-level shared-account engine is a later upgrade for genuinely competing
positions (e.g. cross-sectional long/short).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

ANNUALIZATION = 365.0  # crypto trades 24/7/365


@dataclass(frozen=True)
class LegMetrics:
    name: str
    daily_pnl: pd.Series
    net_usd: float
    sharpe: float
    max_dd_usd: float
    max_dd_pct: float
    days_traded: int


@dataclass(frozen=True)
class PortfolioResult:
    equity: pd.Series
    daily_pnl: pd.Series
    weights: dict[str, float]
    net_usd: float
    sharpe: float
    max_dd_usd: float
    max_dd_pct: float
    correlation: pd.DataFrame
    legs: tuple[LegMetrics, ...]


def trades_to_daily_pnl(trades) -> pd.Series:
    """Sum realised net PnL by EXIT date. Empty series when there are no trades."""
    rows = [(pd.Timestamp(t.exit_ts).tz_convert("UTC").normalize(), float(t.net_pnl_usd))
            for t in trades]
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["date", "pnl"])
    return df.groupby("date")["pnl"].sum().sort_index()


def _sharpe(daily_pnl: pd.Series, base: float) -> float:
    if daily_pnl is None or daily_pnl.empty or base <= 0:
        return 0.0
    r = daily_pnl / base
    sd = r.std(ddof=1)
    if not sd or math.isnan(sd) or sd == 0.0:
        return 0.0
    return float(r.mean() / sd * math.sqrt(ANNUALIZATION))


def _max_dd(equity: pd.Series) -> tuple[float, float]:
    if equity.empty:
        return 0.0, 0.0
    peak = equity.cummax()
    dd = equity - peak
    max_dd_usd = float(dd.min())
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_pct = float((dd / peak).min() * 100.0)
    return max_dd_usd, dd_pct


def combine_portfolio(
    leg_pnls: dict[str, pd.Series],
    *,
    starting_equity: float,
    weighting: str = "equal",
) -> PortfolioResult:
    """Combine per-leg daily-PnL series into a shared-capital portfolio.

    ``weighting``: 'equal' (1/n each) or 'inverse_vol' (risk-parity — weight
    inversely to each leg's daily-PnL volatility). Weights sum to 1 (capital
    share). Per-leg metrics are the standalone 'all-in on this leg' case for a
    fair comparison against the combined book.
    """
    if not leg_pnls:
        raise ValueError("need at least one leg")
    idx = sorted(set().union(*[set(s.index) for s in leg_pnls.values()]))
    aligned = {name: s.reindex(idx).fillna(0.0) for name, s in leg_pnls.items()}
    n = len(leg_pnls)

    if weighting == "equal":
        weights = {name: 1.0 / n for name in leg_pnls}
    elif weighting == "inverse_vol":
        vols = {name: float(aligned[name].std(ddof=1) or 0.0) for name in leg_pnls}
        inv = {name: (1.0 / v if v > 0 else 0.0) for name, v in vols.items()}
        tot = sum(inv.values()) or 1.0
        weights = {name: inv[name] / tot for name in leg_pnls}
    else:
        raise ValueError("weighting must be 'equal' or 'inverse_vol'")

    port_daily = sum(aligned[name] * weights[name] for name in leg_pnls)
    if not isinstance(port_daily, pd.Series):  # single leg / degenerate
        port_daily = pd.Series(port_daily, index=idx)
    equity = starting_equity + port_daily.cumsum()
    dd_usd, dd_pct = _max_dd(equity)

    legs = tuple(
        LegMetrics(
            name=name,
            daily_pnl=aligned[name],
            net_usd=float(aligned[name].sum()),
            sharpe=_sharpe(aligned[name], starting_equity),
            max_dd_usd=_max_dd(starting_equity + aligned[name].cumsum())[0],
            max_dd_pct=_max_dd(starting_equity + aligned[name].cumsum())[1],
            days_traded=int((aligned[name] != 0.0).sum()),
        )
        for name in leg_pnls
    )
    corr = pd.DataFrame(aligned).corr()
    return PortfolioResult(
        equity=equity,
        daily_pnl=port_daily,
        weights=weights,
        net_usd=float(port_daily.sum()),
        sharpe=_sharpe(port_daily, starting_equity),
        max_dd_usd=dd_usd,
        max_dd_pct=dd_pct,
        correlation=corr,
        legs=legs,
    )
