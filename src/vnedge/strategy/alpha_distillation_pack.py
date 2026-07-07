"""Alpha distillation pack.

This is the VNEDGE-native answer to commercial indicator stacks: translate
public visual ideas into causal feature atoms, score them after fee hurdles,
and emit only ordinary backtest intents. It does not copy Pine/TradingView
logic, and it does not bypass promotion, risk, journal, or execution gates.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import ema, zscore
from vnedge.strategy.quant_signal_pack import (
    QuantSignalPackParams,
    add_quant_signal_pack_columns,
)


FEATURE_ATOMS: tuple[str, ...] = (
    "liquidity_sweep",
    "fvg_retest",
    "order_block",
    "squeeze_release",
    "vwap_reclaim",
    "structure_break",
    "trend_trail",
    "profile_reclaim",
    "momentum_impulse",
)


@dataclass(frozen=True)
class IndicatorConcept:
    name: str
    vendor_family: str
    atom: str
    role: str
    priority: int


INDICATOR_CONCEPTS: tuple[IndicatorConcept, ...] = (
    IndicatorConcept("Liquidity Trail Matrix", "WillyAlgo", "trend_trail", "trend trail plus retest", 1),
    IndicatorConcept("Mirage Liquidity Sweep Pro", "WillyAlgo", "liquidity_sweep", "stop-run reclaim", 1),
    IndicatorConcept("Meridian Flow", "WillyAlgo", "momentum_impulse", "flow and momentum filter", 2),
    IndicatorConcept("Synapse Trail Pro", "WillyAlgo", "trend_trail", "adaptive trail", 2),
    IndicatorConcept("Volume-Weighted S/R Zones", "WillyAlgo", "profile_reclaim", "volume support/resistance", 1),
    IndicatorConcept("Liquidity Pools Pro", "WillyAlgo", "liquidity_sweep", "liquidity pool sweep", 1),
    IndicatorConcept("Adaptive Fibonacci Trailing System", "WillyAlgo", "trend_trail", "fib trail", 3),
    IndicatorConcept("Nexus Fusion Engine ML", "WillyAlgo", "momentum_impulse", "multi-factor classifier", 2),
    IndicatorConcept("Self-Aware Trend System", "WillyAlgo", "trend_trail", "trend quality score", 2),
    IndicatorConcept("ABCD Harmonic Projection", "WillyAlgo", "structure_break", "harmonic structure", 3),
    IndicatorConcept("Trade Strategy Calculator", "WillyAlgo", "structure_break", "entry/stop/target plan", 2),
    IndicatorConcept("Pulse Trend Radar", "WillyAlgo", "trend_trail", "trend pulse", 2),
    IndicatorConcept("Breakout Pattern Setup", "WillyAlgo", "structure_break", "breakout setup", 1),
    IndicatorConcept("Daily Volume Profile Pro", "WillyAlgo", "profile_reclaim", "daily profile levels", 1),
    IndicatorConcept("Fibonacci Structure Engine", "WillyAlgo", "structure_break", "structure projection", 3),
    IndicatorConcept("StealthTrail SuperTrend ML Pro", "WillyAlgo", "trend_trail", "supertrend classifier", 2),
    IndicatorConcept("Adaptive Ichimoku Nexus", "WillyAlgo", "trend_trail", "cloud bias", 3),
    IndicatorConcept("Precision Sniper", "WillyAlgo", "momentum_impulse", "high-confluence trigger", 2),
    IndicatorConcept("Smart Breakout Targets", "WillyAlgo", "structure_break", "breakout target plan", 1),
    IndicatorConcept("Adaptive Momentum Fusion", "WillyAlgo", "momentum_impulse", "momentum fusion", 2),
    IndicatorConcept("Swing Volume Profile Pro", "WillyAlgo", "profile_reclaim", "swing profile levels", 2),
    IndicatorConcept("Adaptive Spectral Forecast", "WillyAlgo", "momentum_impulse", "cycle/momentum proxy", 4),
    IndicatorConcept("StealthTrail SuperTrend", "WillyAlgo", "trend_trail", "supertrend trail", 2),
    IndicatorConcept("Adaptive Momentum Classifier", "WillyAlgo", "momentum_impulse", "momentum classifier", 1),
    IndicatorConcept("Phantom Trend Cloud", "WillyAlgo", "trend_trail", "cloud trend", 3),
    IndicatorConcept("SmartTrend Pro", "WillyAlgo", "trend_trail", "trend filter", 2),
    IndicatorConcept("Adaptive Volatility Trend", "WillyAlgo", "squeeze_release", "volatility trend regime", 2),
    IndicatorConcept("Adaptive Trend Pro", "WillyAlgo", "trend_trail", "adaptive trend", 2),
    IndicatorConcept("Smart Money Engine", "WillyAlgo", "structure_break", "SMC structure", 1),
    IndicatorConcept("Squeeze Breakout Pro", "WillyAlgo", "squeeze_release", "compression release", 1),
    IndicatorConcept("Adaptive Pivot Structure", "WillyAlgo", "structure_break", "pivot structure", 2),
    IndicatorConcept("Adaptive Squeeze Momentum Pro", "WillyAlgo", "squeeze_release", "squeeze momentum", 1),
    IndicatorConcept("Auto S/R Channels", "WillyAlgo", "profile_reclaim", "channel support/resistance", 2),
    IndicatorConcept("Automatic Fibonacci Levels", "WillyAlgo", "structure_break", "fib levels", 3),
    IndicatorConcept("FVG Retest Engine / SMC Strategy", "WillyAlgo", "fvg_retest", "FVG retest", 1),
)


@dataclass(frozen=True)
class AlphaDistillationParams:
    quant_params: QuantSignalPackParams = QuantSignalPackParams()
    min_score: float = 8.5
    min_score_delta: float = 1.25
    min_edge_bps: float = 9.0
    maker_edge_floor_bps: float = 9.0
    taker_edge_floor_bps: float = 12.0
    edge_bps_per_score: float = 1.25
    min_exit_quality: float = 70.0
    stop_atr_mult: float = 1.05
    stop_buffer_atr: float = 0.12
    take_profit_r: float = 1.45
    max_take_profit_r: float = 2.30
    min_stop_bps: float = 12.0
    max_stop_bps: float = 180.0
    require_context: bool = True
    require_1m_trigger: bool = True
    allowed_atoms: tuple[str, ...] = ()
    allowed_sides: tuple[str, ...] = ()


def alpha_distillation_warmup_bars(params: AlphaDistillationParams) -> int:
    q = params.quant_params
    return max(
        q.structure_window + 3,
        q.liquidity_window + 3,
        q.atr_window + q.atr_pct_window,
        q.ema_slow + 1,
        q.er_window + 1,
        q.vwap_window + 1,
        q.volume_z_window + 1,
        q.squeeze_window + q.squeeze_pct_window,
    )


def default_alpha_distillation_params() -> AlphaDistillationParams:
    q = QuantSignalPackParams(
        structure_window=32,
        liquidity_window=48,
        atr_window=14,
        atr_pct_window=192,
        ema_fast=12,
        ema_mid=32,
        ema_slow=96,
        er_window=32,
        vwap_window=64,
        volume_z_window=64,
        squeeze_window=32,
        squeeze_pct_window=192,
        squeeze_lookback=8,
        fvg_min_atr=0.15,
        displacement_atr=0.50,
        min_er=0.12,
        min_volume_z=0.20,
        min_score=4.0,
        min_score_delta=0.50,
        min_atr_pct=0.04,
        max_atr_pct=0.97,
        squeeze_max_pct=0.35,
        vwap_extreme_atr=1.10,
        stop_atr_mult=1.10,
        stop_buffer_atr=0.10,
        take_profit_r=1.50,
    )
    return AlphaDistillationParams(quant_params=q)


def add_alpha_distillation_columns(
    candles: pd.DataFrame,
    params: AlphaDistillationParams | None = None,
) -> pd.DataFrame:
    params = params or default_alpha_distillation_params()
    df = add_quant_signal_pack_columns(candles, params.quant_params)

    df["trend_trail_long"] = (
        df["bias_long"]
        & (df["close"] >= df["ema_fast"])
        & (df["ema_fast"] >= df["ema_mid"])
    )
    df["trend_trail_short"] = (
        df["bias_short"]
        & (df["close"] <= df["ema_fast"])
        & (df["ema_fast"] <= df["ema_mid"])
    )
    df["profile_reclaim_long"] = (
        (df["low"] <= df["rolling_vwap"])
        & (df["close"] > df["rolling_vwap"])
        & (df["close"] > df["open"])
        & (df["vwap_distance_atr"].abs() <= 1.75)
    )
    df["profile_reclaim_short"] = (
        (df["high"] >= df["rolling_vwap"])
        & (df["close"] < df["rolling_vwap"])
        & (df["close"] < df["open"])
        & (df["vwap_distance_atr"].abs() <= 1.75)
    )
    df["momentum_impulse_long"] = (
        df["displacement_up"] & df["volume_impulse"] & (df["er"] >= params.quant_params.min_er)
    )
    df["momentum_impulse_short"] = (
        df["displacement_down"] & df["volume_impulse"] & (df["er"] >= params.quant_params.min_er)
    )
    df["liquidity_cluster_long"] = (
        df["sweep_low"]
        | df["bullish_fvg_retest"]
        | df["bull_order_block_proxy"]
        | df["profile_reclaim_long"]
    )
    df["liquidity_cluster_short"] = (
        df["sweep_high"]
        | df["bearish_fvg_retest"]
        | df["bear_order_block_proxy"]
        | df["profile_reclaim_short"]
    )

    _score_side(df, "long", params)
    _score_side(df, "short", params)
    return df


class AlphaDistillationPack(BaseStrategy):
    strategy_id = "alpha_distillation_pack_v1"

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        context_1h: pd.DataFrame | None = None,
        context_4h: pd.DataFrame | None = None,
        trigger_1m: pd.DataFrame | None = None,
        min_score: float = 8.5,
        min_score_delta: float = 1.25,
        min_edge_bps: float = 9.0,
        stop_atr_mult: float = 1.05,
        take_profit_r: float = 1.45,
        require_context: bool = True,
        require_1m_trigger: bool = True,
        allowed_atoms: tuple[str, ...] | list[str] | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        params: AlphaDistillationParams | None = None,
    ) -> None:
        base = params or default_alpha_distillation_params()
        atoms = tuple(allowed_atoms or base.allowed_atoms)
        sides = tuple(allowed_sides or base.allowed_sides)
        _validate_atoms(atoms)
        _validate_sides(sides)
        self.params = replace(
            base,
            min_score=min_score,
            min_score_delta=min_score_delta,
            min_edge_bps=min_edge_bps,
            stop_atr_mult=stop_atr_mult,
            take_profit_r=take_profit_r,
            require_context=require_context,
            require_1m_trigger=require_1m_trigger,
            allowed_atoms=atoms,
            allowed_sides=sides,
        )
        self.funding = funding
        self.context_1h = context_1h
        self.context_4h = context_4h
        self.trigger_1m = trigger_1m
        self.warmup_bars = alpha_distillation_warmup_bars(self.params)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = add_alpha_distillation_columns(candles, self.params).copy()
        df["_decision_ts"] = df["timestamp"] + _timeframe_delta("15m")
        df = _merge_context(df, self.context_1h, "ctx_1h", "1h", self.params)
        df = _merge_context(df, self.context_4h, "ctx_4h", "4h", self.params)
        df = _merge_trigger(df, self.trigger_1m)
        df = _add_context_scores(df)
        df = _refresh_edge_with_context(df, self.params)
        return df.drop(columns=["_decision_ts"])

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        required = (
            "atr",
            "prior_high",
            "prior_low",
            "long_distilled_score",
            "short_distilled_score",
            "long_expected_edge_bps",
            "short_expected_edge_bps",
            "long_exit_quality",
            "short_exit_quality",
        )
        if any(_is_nan(row.get(col)) for col in required):
            return None
        if not bool(row.get("volatility_ok", False)):
            return None

        long_score = float(row["long_distilled_score"])
        short_score = float(row["short_distilled_score"])
        if (
            long_score >= self.params.min_score
            and long_score >= short_score + self.params.min_score_delta
            and self._side_allowed(row, "long")
        ):
            return self._intent(row, "long", long_score, short_score)
        if (
            short_score >= self.params.min_score
            and short_score >= long_score + self.params.min_score_delta
            and self._side_allowed(row, "short")
        ):
            return self._intent(row, "short", long_score, short_score)
        return None

    def _side_allowed(self, row: pd.Series, side: str) -> bool:
        edge = float(row[f"{side}_expected_edge_bps"])
        if self.params.allowed_sides and side not in self.params.allowed_sides:
            return False
        if edge < self.params.min_edge_bps or edge < self.params.maker_edge_floor_bps:
            return False
        if float(row[f"{side}_exit_quality"]) < self.params.min_exit_quality:
            return False
        atom = str(row.get(f"{side}_primary_atom", "confluence"))
        if self.params.allowed_atoms and atom not in self.params.allowed_atoms:
            return False
        if self.params.require_context and float(row.get(f"{side}_context_score", 0.0)) <= 0.0:
            return False
        if self.params.require_1m_trigger and not bool(row.get(f"trigger_1m_{side}", False)):
            return False
        return True

    def _intent(
        self, row: pd.Series, side: str, long_score: float, short_score: float
    ) -> SignalIntent | None:
        close = float(row["close"])
        atr_value = float(row["atr"])
        if close <= 0 or atr_value <= 0:
            return None
        if side == "long":
            stop = self._long_stop(row, close, atr_value)
            if stop <= 0 or stop >= close:
                return None
            risk = close - stop
            target = close + self._target_r(row, side) * risk
        else:
            stop = self._short_stop(row, close, atr_value)
            if stop <= close:
                return None
            risk = stop - close
            target = close - self._target_r(row, side) * risk
        risk_bps = risk / close * 10_000.0
        if risk_bps < self.params.min_stop_bps or risk_bps > self.params.max_stop_bps:
            return None
        return SignalIntent(
            side=side,
            stop_price=stop,
            take_profit_price=target,
            reason=self._reason(row, side, long_score, short_score, risk_bps),
        )

    def _long_stop(self, row: pd.Series, close: float, atr_value: float) -> float:
        structure = min(_float(row.get("prior_low"), close), _float(row.get("low"), close))
        structure_stop = structure - self.params.stop_buffer_atr * atr_value
        atr_stop = close - self.params.stop_atr_mult * atr_value
        return min(structure_stop, atr_stop)

    def _short_stop(self, row: pd.Series, close: float, atr_value: float) -> float:
        structure = max(_float(row.get("prior_high"), close), _float(row.get("high"), close))
        structure_stop = structure + self.params.stop_buffer_atr * atr_value
        atr_stop = close + self.params.stop_atr_mult * atr_value
        return max(structure_stop, atr_stop)

    def _target_r(self, row: pd.Series, side: str) -> float:
        score = float(row[f"{side}_distilled_score"])
        lift = max(0.0, score - self.params.min_score) * 0.08
        return min(self.params.max_take_profit_r, self.params.take_profit_r + lift)

    def _reason(
        self, row: pd.Series, side: str, long_score: float, short_score: float,
        risk_bps: float,
    ) -> str:
        atom = str(row.get(f"{side}_primary_atom", "confluence"))
        route = str(row.get(f"{side}_route", "MAKER_FIRST"))
        active = _active_atoms(row, side)
        return (
            f"alpha_distillation_pack {side} {atom} route={route}; "
            f"score L/S={long_score:.1f}/{short_score:.1f}; "
            f"edge={float(row[f'{side}_expected_edge_bps']):.2f}bps; "
            f"exitQ={float(row[f'{side}_exit_quality']):.0f}; "
            f"risk={risk_bps:.1f}bps; "
            f"context={float(row.get(f'{side}_context_score', 0.0)):+.1f}; "
            f"atoms={','.join(active) or 'none'}; "
            "source=distilled_public_indicator_concepts"
        )


def concept_inventory() -> list[dict]:
    return [
        {
            "name": c.name,
            "vendor_family": c.vendor_family,
            "atom": c.atom,
            "role": c.role,
            "priority": c.priority,
        }
        for c in INDICATOR_CONCEPTS
    ]


def concept_coverage() -> dict:
    coverage: dict[str, list[str]] = {atom: [] for atom in FEATURE_ATOMS}
    for concept in INDICATOR_CONCEPTS:
        coverage.setdefault(concept.atom, []).append(concept.name)
    return coverage


def _score_side(df: pd.DataFrame, side: str, params: AlphaDistillationParams) -> None:
    is_long = side == "long"
    atom_cols = _atom_columns(side)
    for atom, col in atom_cols.items():
        if col not in df:
            df[col] = False

    base = df["long_score"] if is_long else df["short_score"]
    atom_count = sum(df[col].astype(float) for col in atom_cols.values())
    profile = df[atom_cols["profile_reclaim"]].astype(float)
    liquidity = df[atom_cols["liquidity_sweep"]].astype(float)
    fvg = df[atom_cols["fvg_retest"]].astype(float)
    squeeze = df[atom_cols["squeeze_release"]].astype(float)
    trend = df[atom_cols["trend_trail"]].astype(float)
    momentum = df[atom_cols["momentum_impulse"]].astype(float)
    structure = df[atom_cols["structure_break"]].astype(float)
    df[f"{side}_atom_count"] = atom_count
    df[f"{side}_distilled_score"] = (
        base
        + 1.4 * liquidity
        + 1.2 * fvg
        + 1.0 * profile
        + 1.0 * squeeze
        + 0.8 * trend
        + 0.8 * momentum
        + 0.7 * structure
        + 0.3 * atom_count
    )
    df[f"{side}_primary_atom"] = _primary_atom_series(df, side)
    volatility_lift = (df["atr_pct"].clip(0.05, 0.95) - 0.05) * 1.5
    er_lift = df["er"].fillna(0.0).clip(0.0, 1.0) * 1.2
    raw_edge = (
        (df[f"{side}_distilled_score"] - 3.0).clip(lower=0.0)
        * params.edge_bps_per_score
        + volatility_lift
        + er_lift
    )
    df[f"{side}_expected_edge_bps"] = raw_edge
    df[f"{side}_exit_quality"] = (
        35.0
        + 3.0 * df[f"{side}_distilled_score"].clip(0.0, 12.0)
        + 2.0 * df[f"{side}_expected_edge_bps"].clip(0.0, 15.0)
        + 6.0 * (profile + liquidity + fvg).clip(0.0, 1.0)
    ).clip(0.0, 100.0)
    df[f"{side}_route"] = [
        _route(edge, params) for edge in df[f"{side}_expected_edge_bps"]
    ]


def _merge_context(
    base: pd.DataFrame,
    context: pd.DataFrame | None,
    prefix: str,
    timeframe: str,
    params: AlphaDistillationParams,
) -> pd.DataFrame:
    if context is None or context.empty:
        return base
    ctx = add_alpha_distillation_columns(context, params)
    keep = [
        "timestamp",
        "trend_trail_long",
        "trend_trail_short",
        "bos_up",
        "bos_down",
        "choch_up",
        "choch_down",
        "squeeze_release_up",
        "squeeze_release_down",
        "atr_pct",
    ]
    ctx = ctx[keep].copy()
    ctx["_available_ts"] = ctx["timestamp"] + _timeframe_delta(timeframe)
    ctx = ctx.drop(columns=["timestamp"]).rename(
        columns={c: f"{prefix}_{c}" for c in keep if c != "timestamp"}
    )
    out = pd.merge_asof(
        base.sort_values("_decision_ts"),
        ctx.sort_values("_available_ts"),
        left_on="_decision_ts",
        right_on="_available_ts",
        direction="backward",
    )
    return out.drop(columns=["_available_ts"])


def _merge_trigger(base: pd.DataFrame, trigger_1m: pd.DataFrame | None) -> pd.DataFrame:
    if trigger_1m is None or trigger_1m.empty:
        base["trigger_1m_long"] = False
        base["trigger_1m_short"] = False
        return base
    trig = trigger_1m.copy()
    trig["m1_ema_fast"] = ema(trig["close"], 9)
    trig["m1_ema_mid"] = ema(trig["close"], 21)
    trig["m1_momentum_3"] = trig["close"] - trig["close"].shift(3)
    trig["m1_volume_z"] = zscore(trig["volume"], 60)
    trig["trigger_1m_long"] = (
        (trig["close"] > trig["m1_ema_fast"])
        & (trig["m1_ema_fast"] >= trig["m1_ema_mid"])
        & (trig["m1_momentum_3"] > 0)
        & (trig["m1_volume_z"].fillna(0.0) >= -0.50)
    )
    trig["trigger_1m_short"] = (
        (trig["close"] < trig["m1_ema_fast"])
        & (trig["m1_ema_fast"] <= trig["m1_ema_mid"])
        & (trig["m1_momentum_3"] < 0)
        & (trig["m1_volume_z"].fillna(0.0) >= -0.50)
    )
    trig["_available_ts"] = trig["timestamp"] + _timeframe_delta("1m")
    trig = trig[["_available_ts", "trigger_1m_long", "trigger_1m_short"]]
    out = pd.merge_asof(
        base.sort_values("_decision_ts"),
        trig.sort_values("_available_ts"),
        left_on="_decision_ts",
        right_on="_available_ts",
        direction="backward",
    )
    return out.drop(columns=["_available_ts"])


def _add_context_scores(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for side in ("long", "short"):
        score = pd.Series(0.0, index=out.index)
        for prefix, weight in (("ctx_4h", 2.0), ("ctx_1h", 1.5)):
            aligned = _context_aligned(out, prefix, side)
            opposed = _context_aligned(out, prefix, "short" if side == "long" else "long")
            score = score + weight * aligned.astype(float) - weight * opposed.astype(float)
        out[f"{side}_context_score"] = score
    return out


def _refresh_edge_with_context(
    df: pd.DataFrame,
    params: AlphaDistillationParams,
) -> pd.DataFrame:
    out = df.copy()
    for side in ("long", "short"):
        context_lift = out.get(f"{side}_context_score", 0.0)
        trigger_lift = out.get(f"trigger_1m_{side}", False).astype(float)
        out[f"{side}_expected_edge_bps"] = (
            out[f"{side}_expected_edge_bps"]
            + 0.75 * context_lift.clip(-3.5, 3.5)
            + 0.75 * trigger_lift
        )
        out[f"{side}_exit_quality"] = (
            out[f"{side}_exit_quality"]
            + 3.0 * context_lift.clip(0.0, 3.5)
            + 3.0 * trigger_lift
        ).clip(0.0, 100.0)
        out[f"{side}_route"] = [
            _route(edge, params) for edge in out[f"{side}_expected_edge_bps"]
        ]
    return out


def _context_aligned(df: pd.DataFrame, prefix: str, side: str) -> pd.Series:
    if side == "long":
        cols = (
            f"{prefix}_trend_trail_long",
            f"{prefix}_bos_up",
            f"{prefix}_choch_up",
            f"{prefix}_squeeze_release_up",
        )
    else:
        cols = (
            f"{prefix}_trend_trail_short",
            f"{prefix}_bos_down",
            f"{prefix}_choch_down",
            f"{prefix}_squeeze_release_down",
        )
    available = [df[c] for c in cols if c in df]
    if not available:
        return pd.Series(False, index=df.index)
    out = available[0].fillna(False).astype(bool)
    for series in available[1:]:
        out = out | series.fillna(False).astype(bool)
    return out


def _atom_columns(side: str) -> dict[str, str]:
    long = side == "long"
    return {
        "liquidity_sweep": "sweep_low" if long else "sweep_high",
        "fvg_retest": "bullish_fvg_retest" if long else "bearish_fvg_retest",
        "order_block": "bull_order_block_proxy" if long else "bear_order_block_proxy",
        "squeeze_release": "squeeze_release_up" if long else "squeeze_release_down",
        "vwap_reclaim": "vwap_reclaim_long" if long else "vwap_reclaim_short",
        "structure_break": "bos_up" if long else "bos_down",
        "trend_trail": "trend_trail_long" if long else "trend_trail_short",
        "profile_reclaim": "profile_reclaim_long" if long else "profile_reclaim_short",
        "momentum_impulse": "momentum_impulse_long" if long else "momentum_impulse_short",
    }


def _primary_atom(row: pd.Series, side: str) -> str:
    priority = (
        "liquidity_sweep",
        "fvg_retest",
        "profile_reclaim",
        "squeeze_release",
        "order_block",
        "structure_break",
        "trend_trail",
        "momentum_impulse",
        "vwap_reclaim",
    )
    cols = _atom_columns(side)
    for atom in priority:
        if bool(row.get(cols[atom], False)):
            return atom
    return "confluence"


def _primary_atom_series(df: pd.DataFrame, side: str) -> pd.Series:
    priority = (
        "liquidity_sweep",
        "fvg_retest",
        "profile_reclaim",
        "squeeze_release",
        "order_block",
        "structure_break",
        "trend_trail",
        "momentum_impulse",
        "vwap_reclaim",
    )
    cols = _atom_columns(side)
    labels = pd.Series("confluence", index=df.index, dtype="object")
    unassigned = pd.Series(True, index=df.index)
    for atom in priority:
        mask = unassigned & df[cols[atom]].fillna(False).astype(bool)
        labels.loc[mask] = atom
        unassigned = unassigned & ~mask
    return labels


def _active_atoms(row: pd.Series, side: str) -> list[str]:
    cols = _atom_columns(side)
    return [atom for atom, col in cols.items() if bool(row.get(col, False))]


def _route(edge: float, params: AlphaDistillationParams) -> str:
    if edge >= params.taker_edge_floor_bps:
        return "TAKER_ELIGIBLE_RESEARCH"
    if edge >= params.maker_edge_floor_bps:
        return "MAKER_FIRST_RESEARCH"
    return "BLOCKED_FEE_WALL"


def _timeframe_delta(timeframe: str) -> pd.Timedelta:
    if timeframe.endswith("m"):
        return pd.Timedelta(minutes=int(timeframe[:-1]))
    if timeframe.endswith("h"):
        return pd.Timedelta(hours=int(timeframe[:-1]))
    if timeframe.endswith("d"):
        return pd.Timedelta(days=int(timeframe[:-1]))
    raise ValueError(f"unsupported timeframe: {timeframe}")


def _validate_atoms(values: tuple[str, ...]) -> None:
    unknown = sorted(set(values) - set(FEATURE_ATOMS))
    if unknown:
        raise ValueError(f"allowed_atoms contains unknown values: {unknown}")


def _validate_sides(values: tuple[str, ...]) -> None:
    unknown = sorted(set(values) - {"long", "short"})
    if unknown:
        raise ValueError(f"allowed_sides contains unknown values: {unknown}")


def _float(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) else out


def _is_nan(value) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True
