"""Regime detection router -- RANGE / EXPAND / STRESS / UNKNOWN.

Maps market state to the set of strategy ids allowed to arm.  Causal on
closed bars: every feature is computed from bars strictly before the bar
being labelled, so a label can never see its own bar's outcome.  The router
emits no intents and grants no capital permission; it can only *deny*.

Failure is closed in both directions: warmup and non-finite features label
UNKNOWN, and both UNKNOWN and STRESS allow nothing.

Hysteresis keeps labels from flickering bar to bar -- a regime must persist
``hysteresis_bars`` before the router will leave it -- with one deliberate
exception: entry into STRESS is immediate, because a volatility blowout is
the one transition that must not wait for confirmation.

Thresholds are frozen; changing them is a reviewed config change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

import numpy as np
import pandas as pd  # type: ignore[import-untyped]


class Regime(str, Enum):
    RANGE = "range"
    EXPAND = "expand"
    STRESS = "stress"
    UNKNOWN = "unknown"


# Sleeve ids must match registry strategy_id strings.
FAST_SQUEEZE: Final = "squeeze_expansion_breakout_v2"
FAST_VOL_EXP: Final = "volatility_expansion_breakout_v1"
SLOW_RECLAIM: Final = "panic_reversal_v1"
SLOW_PULLBACK: Final = "trend_continuation_v1"
STRUCTURE: Final = "structure_bos_1h"
TICK_ACCEPTED: Final = "tick_accepted_breakout_v1"
SESSION_CONTINUATION: Final = "session_continuation_15m_v1"
SESSION_CONTINUATION_REALTIME: Final = "session_continuation_realtime_v1"
SWEEP_REVERSAL: Final = "liquidity_sweep_reversal_15m_v1"
AVWAP_RECLAIM: Final = "avwap_reclaim_15m_v1"
TREND_PULLBACK: Final = "trend_pullback_1h_v1"
TREND_SQUEEZE: Final = "trend_squeeze_continuation_1h_v1"

FAST_IDS: Final = frozenset({FAST_SQUEEZE, FAST_VOL_EXP})
SLOW_IDS: Final = frozenset({SLOW_RECLAIM, SLOW_PULLBACK, STRUCTURE})
RANGE_NATIVE_IDS: Final = frozenset({AVWAP_RECLAIM, SWEEP_REVERSAL})
EXPAND_NATIVE_IDS: Final = frozenset(
    {
        TICK_ACCEPTED,
        SESSION_CONTINUATION,
        SESSION_CONTINUATION_REALTIME,
        TREND_PULLBACK,
    }
)
TRANSITION_IDS: Final = frozenset({TREND_SQUEEZE})


@dataclass(frozen=True, slots=True)
class RegimeRouterConfig:
    """Frozen thresholds; a change requires a new reviewed config."""

    atr_short: int = 12
    atr_long: int = 48
    vr_expand: float = 1.15
    vr_range: float = 0.90
    er_period: int = 20
    er_trend: float = 0.45
    er_range: float = 0.30
    stress_atr_pct_floor: float = 0.90
    stress_range_bps: float = 120.0
    min_bars: int = 64
    hysteresis_bars: int = 3

    allow_fast_in_range: bool = True
    allow_slow_in_range: bool = False
    allow_fast_in_expand: bool = True
    allow_slow_in_expand: bool = True
    allow_any_in_stress: bool = False

    def __post_init__(self) -> None:
        if self.atr_short < 2 or self.atr_long <= self.atr_short:
            raise ValueError("atr windows are invalid")
        if not 0 < self.vr_range < self.vr_expand:
            raise ValueError("volatility-ratio thresholds must be ascending and positive")
        if not 0 < self.er_range <= self.er_trend < 1:
            raise ValueError("efficiency-ratio thresholds are invalid")
        if not 0 < self.stress_atr_pct_floor <= 1:
            raise ValueError("stress percentile floor must be in (0, 1]")
        if self.stress_range_bps <= 0 or self.min_bars < 2 or self.hysteresis_bars < 1:
            raise ValueError("stress/warmup/hysteresis settings are invalid")


DEFAULT_CONFIG: Final = RegimeRouterConfig()


def build_policy(config: RegimeRouterConfig) -> Mapping[Regime, frozenset[str]]:
    range_set = RANGE_NATIVE_IDS | TRANSITION_IDS | (FAST_IDS if config.allow_fast_in_range else frozenset()) | (
        SLOW_IDS if config.allow_slow_in_range else frozenset()
    )
    expand_set = EXPAND_NATIVE_IDS | TRANSITION_IDS | (FAST_IDS if config.allow_fast_in_expand else frozenset()) | (
        SLOW_IDS if config.allow_slow_in_expand else frozenset()
    )
    return {
        Regime.RANGE: range_set,
        Regime.EXPAND: expand_set,
        Regime.STRESS: (
            FAST_IDS | SLOW_IDS | RANGE_NATIVE_IDS | EXPAND_NATIVE_IDS | TRANSITION_IDS
            if config.allow_any_in_stress else frozenset()
        ),
        Regime.UNKNOWN: frozenset(),
    }


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)


def _efficiency_ratio(close: pd.Series, period: int) -> pd.Series:
    change = (close - close.shift(period)).abs()
    path = close.diff().abs().rolling(period, min_periods=period).sum()
    return change / path.replace(0, np.nan)


@dataclass
class RegimeRouter:
    """Stateful router: classify a bar, then allow or deny sleeve ids."""

    config: RegimeRouterConfig = DEFAULT_CONFIG
    policy: Mapping[Regime, frozenset[str]] = field(default_factory=dict)
    regime: Regime = Regime.UNKNOWN
    bars_in_regime: int = 0
    pending: Regime | None = None
    pending_bars: int = 0

    def __post_init__(self) -> None:
        if not self.policy:
            self.policy = build_policy(self.config)

    def allowed_sleeves(self) -> frozenset[str]:
        return self.policy.get(self.regime, frozenset())

    def allows(self, strategy_id: str) -> bool:
        return strategy_id in self.allowed_sleeves()

    def classify(
        self, vr: float, er: float, bar_range_bps: float, atr_pct_rank: float
    ) -> Regime:
        """Stateless label for one bar's features (no hysteresis)."""
        cfg = self.config
        values = (vr, er, bar_range_bps, atr_pct_rank)
        if any(v is None or not np.isfinite(v) for v in values):
            return Regime.UNKNOWN
        # STRESS is evaluated first: it is the fail-closed state.
        if atr_pct_rank >= cfg.stress_atr_pct_floor or bar_range_bps >= cfg.stress_range_bps:
            return Regime.STRESS
        if vr >= cfg.vr_expand or er >= cfg.er_trend:
            return Regime.EXPAND
        if vr <= cfg.vr_range and er <= cfg.er_range:
            return Regime.RANGE
        return Regime.RANGE

    def update(
        self, *, vr: float, er: float, bar_range_bps: float, atr_pct_rank: float
    ) -> Regime:
        """Online step with confirmation hysteresis.

        A competing regime must persist ``hysteresis_bars`` consecutive bars
        before it is adopted, which damps flicker symmetrically in both
        directions.  STRESS and UNKNOWN bypass confirmation: a volatility
        blowout or a broken feature must take effect on the bar it appears.
        """
        raw = self.classify(vr, er, bar_range_bps, atr_pct_rank)
        if raw is Regime.UNKNOWN:
            self.regime = Regime.UNKNOWN
            self.bars_in_regime = 0
            self.pending = None
            self.pending_bars = 0
            return self.regime
        if raw is Regime.STRESS:
            self.regime = Regime.STRESS
            self.bars_in_regime = 1
            self.pending = None
            self.pending_bars = 0
            return self.regime
        if self.regime in (Regime.UNKNOWN, raw):
            self.regime = raw
            self.bars_in_regime += 1
            self.pending = None
            self.pending_bars = 0
            return self.regime
        if self.pending is raw:
            self.pending_bars += 1
        else:
            self.pending = raw
            self.pending_bars = 1
        if self.pending_bars >= self.config.hysteresis_bars:
            self.regime = raw
            self.bars_in_regime = 1
            self.pending = None
            self.pending_bars = 0
        else:
            self.bars_in_regime += 1
        return self.regime

    def update_from_row(self, row: pd.Series) -> Regime:
        def _get(name: str) -> float:
            value = row.get(name, np.nan)
            try:
                return float(value)
            except (TypeError, ValueError):
                return float("nan")

        return self.update(
            vr=_get("regime_vr"),
            er=_get("regime_er"),
            bar_range_bps=_get("regime_bar_range_bps"),
            atr_pct_rank=_get("regime_atr_pct_rank"),
        )

    def annotate(self, candles: pd.DataFrame) -> pd.DataFrame:
        """Causal feature columns plus the stateless raw label."""
        cfg = self.config
        missing = {"high", "low", "close"}.difference(candles.columns)
        if missing:
            raise ValueError(f"regime router missing columns: {sorted(missing)}")
        out = candles.copy()
        high = pd.to_numeric(out["high"], errors="coerce")
        low = pd.to_numeric(out["low"], errors="coerce")
        close = pd.to_numeric(out["close"], errors="coerce")

        tr = _true_range(high, low, close)
        # shift(1): the bar being labelled never enters its own ATR
        atr_s = tr.shift(1).rolling(cfg.atr_short, min_periods=cfg.atr_short).mean()
        atr_l = tr.shift(1).rolling(cfg.atr_long, min_periods=cfg.atr_long).mean()
        vr = atr_s / atr_l.replace(0, np.nan)
        er = _efficiency_ratio(close.shift(1), cfg.er_period)
        mid = (high.shift(1) + low.shift(1)) / 2.0
        bar_range_bps = (high.shift(1) - low.shift(1)) / mid.replace(0, np.nan) * 10_000.0
        atr_pct = atr_l / close.shift(1).replace(0, np.nan)
        atr_pct_rank = atr_pct.rolling(
            cfg.atr_long * 10, min_periods=cfg.min_bars
        ).rank(pct=True)

        out["regime_vr"] = vr
        out["regime_er"] = er
        out["regime_bar_range_bps"] = bar_range_bps
        out["regime_atr_pct_rank"] = atr_pct_rank
        out["regime_raw"] = [
            self.classify(v, e, b, a).value
            for v, e, b, a in zip(
                vr.tolist(), er.tolist(), bar_range_bps.tolist(), atr_pct_rank.tolist(),
                strict=True,
            )
        ]
        return out


def annotate_with_hysteresis(
    candles: pd.DataFrame, config: RegimeRouterConfig | None = None
) -> pd.DataFrame:
    """Full pass: causal features, hysteretic label, and sleeve permissions."""
    cfg = config or DEFAULT_CONFIG
    router = RegimeRouter(config=cfg)
    out = router.annotate(candles)
    labels: list[str] = []
    allows_fast: list[bool] = []
    allows_slow: list[bool] = []
    for i in range(len(out)):
        if i < cfg.min_bars:
            labels.append(Regime.UNKNOWN.value)
            allows_fast.append(False)
            allows_slow.append(False)
            continue
        regime = router.update_from_row(out.iloc[i])
        allowed = router.allowed_sleeves()
        labels.append(regime.value)
        allows_fast.append(bool(allowed & FAST_IDS))
        allows_slow.append(bool(allowed & SLOW_IDS))
    out["regime"] = labels
    out["regime_allows_fast"] = allows_fast
    out["regime_allows_slow"] = allows_slow
    return out


def annotate_strategy_route(
    candles: pd.DataFrame,
    strategy_id: str,
    config: RegimeRouterConfig | None = None,
) -> pd.DataFrame:
    """Attach the denial-only decision for one exact scanner registration."""
    cfg = config or DEFAULT_CONFIG
    out = annotate_with_hysteresis(candles, cfg)
    policy = build_policy(cfg)
    out["regime_route_allowed"] = [
        strategy_id in policy.get(Regime(str(label)), frozenset())
        for label in out["regime"]
    ]
    return out


EXIT_PROFILE: Final[Mapping[Regime, str]] = {
    Regime.RANGE: "scalp",
    Regime.EXPAND: "swing",
    Regime.STRESS: "flat",
    Regime.UNKNOWN: "flat",
}
