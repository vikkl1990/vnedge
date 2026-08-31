"""Mechanism features mined from the audited indicator corpora.

The 2026-08 indicator audits (LuxAlgo 409, fmzquant 5,807, AlgoAlpha 131
scripts; replays in ``data/strategies/ai/`` and the burn ledger) ended with
one standing conclusion: none of these mechanisms carries standalone
after-cost alpha, but several describe *where liquidity and reaction points
sit* better than raw OHLCV does. This module is that demotion made concrete —
each mechanism enters the ML plane as a FEATURE for the meta-labeler, a far
lower bar than alpha: it only has to carry marginal information about whether
a rule-based signal wins after costs.

Every feature at bar i is computable from bars 0..i only, same contract as
``feature_matrix``. "Bars since"/distance features saturate at ``far_cap``
instead of going NaN when the event has never occurred, so a quiet series
does not purge the whole matrix (same reasoning as the funding_z guard).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from vnedge.strategy.indicators import rolling_percentile


@dataclass(frozen=True)
class MechanismParams:
    swing_wing: int = 10          # confirmed-pivot wing (LuxAlgo/AlgoAlpha structure)
    break_window: int = 10        # structure-break lookback (order-block context)
    sweep_window: int = 14        # liquidity-sweep pivot window (LuxAlgo)
    sweep_min_atr: float = 0.10   # minimum wick penetration, in ATRs
    fvg_min_atr: float = 0.25     # minimum gap width, in ATRs (LuxAlgo FVG)
    fvg_max_age: int = 72
    donchian_window: int = 20     # channel position (turtle/FMZ family)
    st_atr_window: int = 10       # supertrend recursion (AlgoAlpha/FMZ family)
    st_mult: float = 3.0
    vol_pct_window: int = 100     # honest "ML SuperTrend": rolling ATR percentile
    engulf_streak: int = 3        # exhaustion-engulfing precondition
    far_cap: float = 500.0        # saturation for bars-since / no-event distances

    @property
    def warmup_bars(self) -> int:
        return max(
            2 * self.swing_wing + 1,
            self.vol_pct_window,
            self.donchian_window,
            self.sweep_window,
            self.st_atr_window,
        ) + 2


#: appended to feature_matrix.FEATURE_COLUMNS — order is part of the contract
MECHANISM_FEATURE_COLUMNS = [
    "dist_swing_high_atr", "dist_swing_low_atr",
    "swing_high_age", "swing_low_age",
    "bars_since_break_up", "bars_since_break_down", "break_disp_atr",
    "bull_fvg_open", "dist_bull_fvg_atr", "bear_fvg_open", "dist_bear_fvg_atr",
    "bars_since_sweep_down", "bars_since_sweep_up",
    "donchian_pos", "donchian_width_atr",
    "st_dir", "bars_since_st_flip",
    "atr_pctile", "rsi2",
    "bull_engulf", "bear_engulf",
]


def _bars_since(event: pd.Series, cap: float) -> pd.Series:
    """Bars elapsed since the last True, saturating at ``cap`` (never NaN
    once any bar exists — "never happened" reads as "long ago")."""
    positions = pd.Series(np.arange(len(event), dtype=float), index=event.index)
    last = positions.where(event.fillna(False)).ffill()
    return (positions - last).fillna(cap).clip(upper=cap)


def _grouped_prior_cummin(values: pd.Series, group_id: pd.Series) -> pd.Series:
    def _f(s: pd.Series) -> pd.Series:
        return s.shift(1).cummin()
    grouped = values.groupby(group_id).apply(_f)
    if isinstance(grouped.index, pd.MultiIndex):
        grouped = grouped.reset_index(level=0, drop=True)
    # Rows before the first event carry a NaN group id and are dropped by
    # groupby; reindex restores full alignment (NaN there, by construction).
    return grouped.reindex(values.index)


def _grouped_prior_cummax(values: pd.Series, group_id: pd.Series) -> pd.Series:
    def _f(s: pd.Series) -> pd.Series:
        return s.shift(1).cummax()
    grouped = values.groupby(group_id).apply(_f)
    if isinstance(grouped.index, pd.MultiIndex):
        grouped = grouped.reset_index(level=0, drop=True)
    return grouped.reindex(values.index)


def add_mechanism_features(
    df: pd.DataFrame, params: MechanismParams = MechanismParams()
) -> pd.DataFrame:
    """Append MECHANISM_FEATURE_COLUMNS to a frame that already has ``atr``."""
    out = df.copy()
    close, high, low = out["close"], out["high"], out["low"]
    open_ = out["open"]
    atr = out["atr"]
    safe_atr = atr.where(atr > 0)
    positions = pd.Series(np.arange(len(out), dtype=float), index=out.index)
    cap = params.far_cap

    # --- confirmed swing structure (SMC/BOS family) ------------------------
    wing = params.swing_wing
    span = 2 * wing + 1
    sh_val = high.shift(wing)
    sh_conf = sh_val.eq(high.rolling(span).max()).fillna(False)
    sl_val = low.shift(wing)
    sl_conf = sl_val.eq(low.rolling(span).min()).fillna(False)
    swing_high = sh_val.where(sh_conf).ffill()
    swing_low = sl_val.where(sl_conf).ffill()
    out["dist_swing_high_atr"] = ((swing_high - close) / safe_atr).clip(-cap, cap)
    out["dist_swing_low_atr"] = ((close - swing_low) / safe_atr).clip(-cap, cap)
    out["swing_high_age"] = _bars_since(sh_conf, cap)
    out["swing_low_age"] = _bars_since(sl_conf, cap)

    # --- structure breaks + displacement (order-block context) -------------
    prev_high = high.rolling(params.break_window).max().shift(1)
    prev_low = low.rolling(params.break_window).min().shift(1)
    break_up = (close > prev_high).fillna(False)
    break_down = (close < prev_low).fillna(False)
    out["bars_since_break_up"] = _bars_since(break_up, cap)
    out["bars_since_break_down"] = _bars_since(break_down, cap)
    bar_range_atr = (high - low) / safe_atr
    disp = bar_range_atr.where(break_up | break_down)
    out["break_disp_atr"] = disp.ffill().fillna(0.0).clip(upper=cap)

    # --- fair value gaps (unfilled, freshest per side) ----------------------
    high_2, low_2 = high.shift(2), low.shift(2)
    bull_formed = ((low > high_2) & ((low - high_2) >= params.fvg_min_atr * atr)).fillna(False)
    bear_formed = ((high < low_2) & ((low_2 - high) >= params.fvg_min_atr * atr)).fillna(False)

    bull_id = positions.where(bull_formed).ffill()
    bull_bottom = high_2.where(bull_formed).ffill()
    bull_age = _bars_since(bull_formed, cap)
    bull_prior_low = _grouped_prior_cummin(low, bull_id)
    bull_unfilled = (
        bull_bottom.notna()
        & (bull_age <= params.fvg_max_age)
        & (bull_prior_low.fillna(np.inf) > bull_bottom)
        & (low > bull_bottom)
    )
    out["bull_fvg_open"] = bull_unfilled.astype(float)
    out["dist_bull_fvg_atr"] = (
        ((close - bull_bottom) / safe_atr).where(bull_unfilled).fillna(cap).clip(-cap, cap)
    )

    bear_id = positions.where(bear_formed).ffill()
    bear_top = low_2.where(bear_formed).ffill()
    bear_age = _bars_since(bear_formed, cap)
    bear_prior_high = _grouped_prior_cummax(high, bear_id)
    bear_unfilled = (
        bear_top.notna()
        & (bear_age <= params.fvg_max_age)
        & (bear_prior_high.fillna(-np.inf) < bear_top)
        & (high < bear_top)
    )
    out["bear_fvg_open"] = bear_unfilled.astype(float)
    out["dist_bear_fvg_atr"] = (
        ((bear_top - close) / safe_atr).where(bear_unfilled).fillna(cap).clip(-cap, cap)
    )

    # --- liquidity sweeps (wick through prior extreme, close back inside) ---
    pivot_low_prev = low.rolling(params.sweep_window).min().shift(1)
    pivot_high_prev = high.rolling(params.sweep_window).max().shift(1)
    swept_down = (
        (low < pivot_low_prev)
        & (close > pivot_low_prev)
        & ((pivot_low_prev - low) >= params.sweep_min_atr * atr)
    ).fillna(False)
    swept_up = (
        (high > pivot_high_prev)
        & (close < pivot_high_prev)
        & ((high - pivot_high_prev) >= params.sweep_min_atr * atr)
    ).fillna(False)
    out["bars_since_sweep_down"] = _bars_since(swept_down, cap)
    out["bars_since_sweep_up"] = _bars_since(swept_up, cap)

    # --- channel position (turtle/donchian family) --------------------------
    d_high = high.rolling(params.donchian_window).max()
    d_low = low.rolling(params.donchian_window).min()
    width = d_high - d_low
    out["donchian_pos"] = ((close - d_low) / width.where(width > 0)).clip(0.0, 1.0)
    out["donchian_width_atr"] = (width / safe_atr).clip(upper=cap)

    # --- supertrend state (causal forward recursion) ------------------------
    st_atr = atr.rolling(params.st_atr_window).mean()
    hl2 = (high + low) / 2.0
    ub = (hl2 + params.st_mult * st_atr).to_numpy(dtype=float)
    lb = (hl2 - params.st_mult * st_atr).to_numpy(dtype=float)
    closes = close.to_numpy(dtype=float)
    n = len(out)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    st_dir = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(ub[i]) or np.isnan(lb[i]):
            continue
        if i == 0 or np.isnan(upper[i - 1]):
            upper[i], lower[i], st_dir[i] = ub[i], lb[i], 1.0
            continue
        upper[i] = ub[i] if ub[i] < upper[i - 1] or closes[i - 1] > upper[i - 1] else upper[i - 1]
        lower[i] = lb[i] if lb[i] > lower[i - 1] or closes[i - 1] < lower[i - 1] else lower[i - 1]
        if st_dir[i - 1] > 0:
            st_dir[i] = -1.0 if closes[i] < lower[i] else 1.0
        else:
            st_dir[i] = 1.0 if closes[i] > upper[i] else -1.0
    out["st_dir"] = st_dir
    dir_series = pd.Series(st_dir, index=out.index)
    flipped = dir_series.ne(dir_series.shift(1)) & dir_series.notna() & dir_series.shift(1).notna()
    out["bars_since_st_flip"] = _bars_since(flipped, cap)

    # --- volatility regime: the honest "ML SuperTrend" ----------------------
    # (k-means over trailing ATR is functionally a rolling percentile bucket)
    out["atr_pctile"] = rolling_percentile(atr, params.vol_pct_window)

    # --- fast reversion state (the one CANDIDATE family) --------------------
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=0.5, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=0.5, adjust=False).mean()
    rs = gain / loss.where(loss > 0)
    out["rsi2"] = (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)

    # --- exhaustion engulfing flags ------------------------------------------
    down = (close < close.shift(1)).astype(float)
    up = (close > close.shift(1)).astype(float)
    k = params.engulf_streak
    down_leg = down.shift(1).rolling(k).sum().ge(k)
    up_leg = up.shift(1).rolling(k).sum().ge(k)
    open_prev, close_prev = open_.shift(1), close.shift(1)
    out["bull_engulf"] = (
        down_leg & (close_prev < open_prev) & (close > open_)
        & (open_ <= close_prev) & (close >= open_prev)
    ).fillna(False).astype(float)
    out["bear_engulf"] = (
        up_leg & (close_prev > open_prev) & (close < open_)
        & (open_ >= close_prev) & (close <= open_prev)
    ).fillna(False).astype(float)

    return out
