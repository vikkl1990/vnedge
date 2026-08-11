"""Cross-sectional carry factor: causal score → market-neutral book → net P&L.

Strictly causal: the score at day t uses only funding printed on or before t;
the book is held from t+1. Cost is charged on the fraction turned over at each
rebalance. Carry income is included (a short collects positive funding).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class CarryConfig:
    lookback_days: int = 3      # trailing window for the funding score
    k: int = 3                  # legs per side (long k lowest, short k highest)
    rebalance_days: int = 7     # weekly
    cost_bps: float = 14.0      # per unit of turnover at each rebalance
    warmup_days: int = 31       # skip the leading window before trading


def funding_score(funding: pd.DataFrame, t: int, lookback: int) -> pd.Series:
    """Mean daily funding over day t and the `lookback` prior days — causal.

    Window is ``[t-lookback, t]`` inclusive (lookback+1 rows), reproducing the
    exact frozen `xsect_carry_v1` construction that was judged on the sealed
    tail. Do NOT change this window without a fresh pre-registration — the
    2025-07→2026-06 tail is already spent for this factor.
    """
    return funding.iloc[max(0, t - lookback):t + 1].mean()


def market_neutral_book(scores: pd.Series, k: int) -> pd.Series:
    """Long the k lowest-funding names, short the k highest, dollar-neutral.

    Requires 2*k <= len(scores) or the long/short sets would overlap.
    """
    if 2 * k > len(scores):
        raise ValueError(f"2*k ({2 * k}) exceeds universe size ({len(scores)})")
    order = scores.sort_values()          # ascending → lowest funding first
    w = pd.Series(0.0, index=scores.index)
    w[order.index[:k]] = 1.0 / k          # long low funding
    w[order.index[-k:]] = -1.0 / k        # short high funding
    return w


def factor_pnl(close: pd.DataFrame, funding: pd.DataFrame, cfg: CarryConfig) -> pd.Series:
    """Daily net P&L series (price move + funding carry − turnover cost)."""
    ret = close.pct_change()
    dates = close.index
    out: dict[pd.Timestamp, float] = {}
    w_prev: pd.Series | None = None
    for t in range(cfg.warmup_days, len(dates) - 1):
        if (t - cfg.warmup_days) % cfg.rebalance_days == 0:
            sc = funding_score(funding, t, cfg.lookback_days)
            if sc.isna().any():
                out[dates[t + 1]] = 0.0
                continue
            w = market_neutral_book(sc, cfg.k)
            turnover = (w - (w_prev if w_prev is not None else 0)).abs().sum()
            cost = turnover * cfg.cost_bps / 1e4
            w_prev = w
        else:
            cost = 0.0
        r = ret.iloc[t + 1].reindex(w_prev.index).fillna(0.0)
        f = funding.iloc[t + 1].reindex(w_prev.index).fillna(0.0)
        px = float((w_prev * r).sum())
        carry = float((-w_prev * f).sum())   # short (w<0) collects +funding
        out[dates[t + 1]] = px + carry - cost
    return pd.Series(out).sort_index()
