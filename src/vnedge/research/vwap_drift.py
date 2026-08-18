"""VWAP drift analysis -- causal measurement of price versus VWAP.

Research-only measurement: no orders, no capital permission, no signal.
Its job is to decide, on evidence, whether the squeeze lane's S4 veto
(fire long only above the rolling 24h VWAP, short only below) earns its
place or quietly costs trades.

Drift definitions (bps unless noted)::

    signed_bps = (close - vwap) / vwap * 1e4
    abs_bps    = abs(signed_bps)
    delta_bps  = signed_bps[t] - signed_bps[t - 1]

The rolling VWAP matches the squeeze S4 definition exactly: sum(p*v) / sum(v)
over the prior ``vwap_bars`` bars, current bar excluded, so nothing here can
leak the bar being measured.  Forward-return columns are labels: they are
known only at t+h and must never be fed back into a live decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class VwapDriftConfig:
    vwap_bars: int = 288  # 24h of 5m
    forward_horizons: tuple[int, ...] = (1, 3, 6, 12, 24)
    min_periods: int = 48
    bucket_edges_bps: tuple[float, ...] = (-50.0, -20.0, -10.0, 0.0, 10.0, 20.0, 50.0)

    def __post_init__(self) -> None:
        if self.vwap_bars < 2 or self.min_periods < 1:
            raise ValueError("vwap window settings are invalid")
        if not self.forward_horizons or any(h < 1 for h in self.forward_horizons):
            raise ValueError("forward horizons must be positive bar counts")
        if list(self.bucket_edges_bps) != sorted(self.bucket_edges_bps):
            raise ValueError("bucket edges must be ascending")


def rolling_vwap(
    close: pd.Series,
    volume: pd.Series,
    window: int,
    *,
    min_periods: int | None = None,
) -> pd.Series:
    """Causal VWAP: at bar i uses bars [i-window, i) -- current bar excluded."""
    periods = min_periods if min_periods is not None else max(1, window // 4)
    pv = (close * volume).shift(1)
    vol = volume.shift(1)
    sum_pv = pv.rolling(window, min_periods=periods).sum()
    sum_v = vol.rolling(window, min_periods=periods).sum()
    return sum_pv / sum_v.replace(0, np.nan)


def signed_drift_bps(close: pd.Series, vwap: pd.Series) -> pd.Series:
    return (close - vwap) / vwap * 10_000.0


def forward_return_bps(close: pd.Series, horizon: int) -> pd.Series:
    """Label: return from bar t close to bar t+h close (known only at t+h)."""
    return (close.shift(-horizon) / close - 1.0) * 10_000.0


def annotate_vwap_drift(
    candles: pd.DataFrame,
    config: VwapDriftConfig | None = None,
) -> pd.DataFrame:
    cfg = config or VwapDriftConfig()
    missing = {"close", "volume"}.difference(candles.columns)
    if missing:
        raise ValueError(f"vwap drift missing columns: {sorted(missing)}")
    out = candles.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    volume = pd.to_numeric(out["volume"], errors="coerce")
    vwap = rolling_vwap(close, volume, cfg.vwap_bars, min_periods=cfg.min_periods)
    signed = signed_drift_bps(close, vwap)
    out["vwap"] = vwap
    out["vwap_drift_bps"] = signed
    out["vwap_abs_drift_bps"] = signed.abs()
    out["vwap_drift_delta_bps"] = signed.diff()
    # NaN-preserving side flag: unknown VWAP must not read as "below".
    out["above_vwap"] = np.where(signed.isna(), np.nan, (close > vwap).astype(float))
    return out


def drift_bucket_table(
    df: pd.DataFrame,
    *,
    config: VwapDriftConfig | None = None,
) -> pd.DataFrame:
    """Mean/median forward returns by VWAP-drift bucket (measurement only)."""
    cfg = config or VwapDriftConfig()
    if "vwap_drift_bps" not in df.columns:
        df = annotate_vwap_drift(df, cfg)
    close = pd.to_numeric(df["close"], errors="coerce")
    drift = df["vwap_drift_bps"]
    edges = list(cfg.bucket_edges_bps)
    labels = [f"<{edges[0]:g}"]
    labels += [f"{edges[i - 1]:g}..{edges[i]:g}" for i in range(1, len(edges))]
    labels.append(f">{edges[-1]:g}")
    cat = pd.cut(drift, bins=[-np.inf, *edges, np.inf], labels=labels, right=True)
    forwards = {h: forward_return_bps(close, h) for h in cfg.forward_horizons}

    rows: list[dict[str, Any]] = []
    for label in labels:
        mask = cat == label
        count = int(mask.sum())
        row: dict[str, Any] = {"bucket": label, "n": count}
        row["mean_drift_bps"] = float(drift[mask].mean()) if count else float("nan")
        for horizon, fwd in forwards.items():
            valid = mask & fwd.notna()
            row[f"fwd_{horizon}_mean_bps"] = (
                float(fwd[valid].mean()) if valid.any() else float("nan")
            )
            row[f"fwd_{horizon}_n"] = int(valid.sum())
        rows.append(row)
    return pd.DataFrame(rows)


def side_of_vwap_hit_rate(
    df: pd.DataFrame,
    *,
    forward_bars: int = 12,
    long_when_above: bool = True,
) -> dict[str, float]:
    """If you only take the VWAP side, how often is the forward move right?"""
    if "vwap" not in df.columns:
        df = annotate_vwap_drift(df)
    close = pd.to_numeric(df["close"], errors="coerce")
    fwd = forward_return_bps(close, forward_bars)
    above = df["above_vwap"]
    known = above.notna()
    selected = known & (above > 0 if long_when_above else above == 0)
    directional = fwd if long_when_above else -fwd
    valid = selected & directional.notna()
    if not valid.any():
        return {"n": 0.0, "hit_rate": float("nan"), "mean_bps": float("nan")}
    returns = directional[valid]
    return {
        "n": float(valid.sum()),
        "hit_rate": float((returns > 0).mean()),
        "mean_bps": float(returns.mean()),
        "median_bps": float(returns.median()),
    }


def expansion_day_drift_profile(
    df: pd.DataFrame,
    *,
    day_key: pd.Series | None = None,
    range_bps_threshold: float = 200.0,
) -> pd.DataFrame:
    """Per UTC day: range, open/close drift, stretch, and time spent above."""
    if "vwap_drift_bps" not in df.columns:
        df = annotate_vwap_drift(df)
    out = df.copy()
    if day_key is None:
        if not isinstance(out.index, pd.DatetimeIndex):
            raise ValueError("DatetimeIndex or explicit day_key required")
        index = out.index.tz_convert("UTC") if out.index.tz else out.index
        day_key = pd.Series(index.date, index=out.index)
    close = pd.to_numeric(out["close"], errors="coerce")
    high = pd.to_numeric(out["high"], errors="coerce") if "high" in out else close
    low = pd.to_numeric(out["low"], errors="coerce") if "low" in out else close

    rows = []
    for day, group in out.groupby(day_key):
        first_close = float(group["close"].iloc[0])
        top = float(high.loc[group.index].max())
        bottom = float(low.loc[group.index].min())
        range_bps = (top - bottom) / first_close * 10_000.0 if first_close else float("nan")
        drift = group["vwap_drift_bps"].dropna()
        rows.append(
            {
                "day": str(day),
                "range_bps": range_bps,
                "expansion": bool(range_bps >= range_bps_threshold),
                "drift_open": float(drift.iloc[0]) if len(drift) else float("nan"),
                "drift_close": float(drift.iloc[-1]) if len(drift) else float("nan"),
                "drift_min": float(drift.min()) if len(drift) else float("nan"),
                "drift_max": float(drift.max()) if len(drift) else float("nan"),
                "frac_above_vwap": float(group["above_vwap"].mean(skipna=True)),
                "n_bars": int(len(group)),
            }
        )
    return pd.DataFrame(rows)


def s4_filter_diagnostic(
    df: pd.DataFrame,
    *,
    signal_long: pd.Series,
    signal_short: pd.Series,
    forward_bars: int = 12,
    config: VwapDriftConfig | None = None,
) -> dict[str, Any]:
    """Raw fires versus VWAP-side-filtered fires -- is the veto helping?"""
    cfg = config or VwapDriftConfig()
    if "above_vwap" not in df.columns:
        df = annotate_vwap_drift(df, cfg)
    above = df["above_vwap"]
    long_raw = signal_long.fillna(False).astype(bool)
    short_raw = signal_short.fillna(False).astype(bool)
    long_s4 = long_raw & (above > 0)
    short_s4 = short_raw & (above == 0)
    close = pd.to_numeric(df["close"], errors="coerce")
    fwd = forward_return_bps(close, forward_bars)

    def _stats(mask: pd.Series, directional: pd.Series) -> dict[str, float]:
        valid = mask & directional.notna()
        if not valid.any():
            return {"n": 0.0, "mean_fwd_bps": float("nan"), "hit": float("nan")}
        returns = directional[valid]
        return {
            "n": float(valid.sum()),
            "mean_fwd_bps": float(returns.mean()),
            "hit": float((returns > 0).mean()),
        }

    return {
        "long_raw": _stats(long_raw, fwd),
        "long_s4": _stats(long_s4, fwd),
        "short_raw": _stats(short_raw, -fwd),
        "short_s4": _stats(short_s4, -fwd),
        "vetoed_longs": int((long_raw & ~(above > 0)).sum()),
        "vetoed_shorts": int((short_raw & ~(above == 0)).sum()),
    }
