"""Confirmed 1h break of structure, pre-registered for research only.

Closed bars and causally confirmed swings only. ``on_closed_candle`` emits a
non-executable research intent; the ``BaseStrategy`` adapter exposes a virtual
backtest signal only after the existing CostGate approves it. Registry and
promotion controls remain separate, mandatory choke points.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Literal

import pandas as pd

from vnedge.data.candles import Candle
from vnedge.data.regime_context import RegimeContext, RegimeLabel
from vnedge.data.structure import (
    StructureEvent,
    StructureEventType,
    StructureState,
    StructureTrend,
    SwingPairState,
    build_structure_state,
    classify_hh_hl,
    detect_bos_choch,
    structure_from_bars,
    structure_labels,
)
from vnedge.data.structure_mtf import (
    MTF_PARAMS,
    Alignment,
    MTFParams,
    align_structure,
    build_mtf_snapshot,
    fully_closed_htf,
)
from vnedge.data.swings import SwingAnchor, SwingDetectConfig, SwingKind, detect_swings
from vnedge.data.vwap import DualAVWAPBias, dual_avwap_bias, vwap_from_sums
from vnedge.risk.cost_gate import CostGate, CostGateResult, CostProfile
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.base_strategy import SignalIntent as BacktestSignalIntent
from vnedge.strategy.regime import merge_funding


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


Bias = Literal["strong_long", "strong_short", "between", "n/a", "unavailable"]
DataQuality = Literal["ok", "degraded", "gap"]


@dataclass(frozen=True, slots=True)
class StructureBosParams:
    """Frozen registration. A parameter change requires a new strategy ID."""

    left: int = 3
    right: int = 3
    strict_swings: bool = True
    break_buffer_bps: Decimal = Decimal(5)
    stop_buffer_bps: Decimal = Decimal(10)
    atr_period: int = 14
    use_atr_stop_cap: bool = True
    atr_stop_mult: Decimal = Decimal("1.5")
    max_hold_hours: int = 48
    require_bias_not_against: bool = True
    min_bars: int = 50
    cost_edge_reward_r: Decimal = Decimal("1.5")
    min_net_edge_bps: Decimal = Decimal(4)
    min_room_cost_multiple: Decimal = Decimal("1.5")
    entry_urgency: Literal["taker"] = "taker"
    mtf: MTFParams = MTF_PARAMS

    def __post_init__(self) -> None:
        if self.left < 1 or self.right < 1:
            raise ValueError("swing windows must be positive")
        if self.break_buffer_bps < 0 or self.stop_buffer_bps < 0:
            raise ValueError("break and stop buffers cannot be negative")
        if self.atr_period < 1 or self.atr_stop_mult <= 0:
            raise ValueError("ATR settings must be positive")
        if self.max_hold_hours < 1 or self.min_bars < self.left + self.right + 1:
            raise ValueError("time stop or minimum history is invalid")
        if (
            self.cost_edge_reward_r <= 0
            or self.min_net_edge_bps < 0
            or self.min_room_cost_multiple <= 0
        ):
            raise ValueError("cost-edge settings are invalid")


PARAMS = StructureBosParams()

strategy_id = "structure_bos_1h"
eligibility = "RESEARCH_ONLY"
timeframe = "1h"
params = PARAMS
cost_profile = CostProfile.SWING

STRATEGY_SPEC: Mapping[str, object] = MappingProxyType(
    {
        "strategy_id": strategy_id,
        "eligibility": eligibility,
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": timeframe,
        "params": PARAMS,
        "mtf_params": MTF_PARAMS,
    }
)


@dataclass(frozen=True, slots=True)
class StructureContext:
    """Measurement context; no field can grant execution permission."""

    dual_avwap_bias: Bias = "n/a"
    data_quality: DataQuality = "ok"
    regime_1h: RegimeContext | None = None
    regime_4h: RegimeContext | None = None


@dataclass(frozen=True, slots=True)
class StructureBosIntent:
    """Non-executable research artifact emitted by ``on_closed_candle``."""

    strategy_id: str
    signal_id: str
    symbol: str
    side: Side
    ts: datetime
    entry_ref: Decimal
    stop_ref: Decimal
    time_stop: datetime
    reason: str
    meta: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class StructureBosEvaluation:
    """Operator-visible candidate plus its non-mutating CostGate report."""

    candidate: BacktestSignalIntent | None
    research_intent: StructureBosIntent | None
    cost: CostGateResult | None
    accepted: bool
    reason: str


def _bps_offset(price: Decimal, bps: Decimal, direction: int) -> Decimal:
    """Offset ``price`` by bps; direction is +1 up or -1 down."""
    if direction not in {-1, 1}:
        raise ValueError("bps offset direction must be -1 or +1")
    return price * (Decimal(1) + Decimal(direction) * bps / Decimal(10_000))


def _signal_id(symbol: str, ts: datetime, side: Side) -> str:
    """Deterministic identity for replay idempotency and de-duplication."""
    return f"{strategy_id}:{symbol}:{ts.strftime('%Y%m%d%H%M')}:{side.value}"


def _true_range_at(bars: Sequence[Candle], index: int) -> Decimal:
    bar = bars[index]
    if index == 0:
        return bar.high - bar.low
    previous_close = bars[index - 1].close
    return max(
        bar.high - bar.low,
        abs(bar.high - previous_close),
        abs(bar.low - previous_close),
    )


def _atr(bars: Sequence[Candle], period: int) -> Decimal | None:
    """Simple mean of causal true ranges over the last ``period`` closed bars."""
    if period < 1 or len(bars) < period:
        return None
    start = len(bars) - period
    values = [_true_range_at(bars, index) for index in range(start, len(bars))]
    return sum(values, Decimal(0)) / Decimal(period)


def _long_stop(
    bars: Sequence[Candle], entry: Decimal, swing_low: Decimal, p: StructureBosParams
) -> Decimal:
    swing_stop = _bps_offset(swing_low, p.stop_buffer_bps, -1)
    if not p.use_atr_stop_cap:
        return swing_stop
    atr = _atr(bars, p.atr_period)
    if atr is None or atr <= 0:
        return swing_stop
    return max(swing_stop, entry - p.atr_stop_mult * atr)


def _short_stop(
    bars: Sequence[Candle], entry: Decimal, swing_high: Decimal, p: StructureBosParams
) -> Decimal:
    swing_stop = _bps_offset(swing_high, p.stop_buffer_bps, 1)
    if not p.use_atr_stop_cap:
        return swing_stop
    atr = _atr(bars, p.atr_period)
    if atr is None or atr <= 0:
        return swing_stop
    return min(swing_stop, entry + p.atr_stop_mult * atr)


def _research_intent(
    *,
    symbol: str,
    side: Side,
    as_of: datetime,
    entry: Decimal,
    stop: Decimal,
    level: Decimal,
    opposite_swing: Decimal,
    bias: Bias,
    trend: Literal["up", "down"],
    p: StructureBosParams,
) -> StructureBosIntent:
    return StructureBosIntent(
        strategy_id=strategy_id,
        signal_id=_signal_id(symbol, as_of, side),
        symbol=symbol,
        side=side,
        ts=as_of,
        entry_ref=entry,
        stop_ref=stop,
        time_stop=as_of + timedelta(hours=p.max_hold_hours),
        reason="bos_up_break_swing_high" if side == Side.LONG else "bos_down_break_swing_low",
        meta=MappingProxyType(
            {
                "trend": trend,
                "break_level": str(level),
                "opposite_swing": str(opposite_swing),
                "bias": bias,
                "tf": timeframe,
                "break_buffer_bps": str(p.break_buffer_bps),
                "cost_edge_reward_r": str(p.cost_edge_reward_r),
            }
        ),
    )


def evaluate_bos_intent(
    intent: StructureBosIntent,
    cost_gate: CostGate,
    *,
    current_funding_rate: Decimal | float | str = Decimal(0),
) -> CostGateResult:
    """Evaluate one raw research intent with the frozen 1.5R edge hypothesis."""
    risk = abs(intent.entry_ref - intent.stop_ref)
    edge_bps = risk * PARAMS.cost_edge_reward_r * Decimal(10_000) / intent.entry_ref
    holding_seconds = int((intent.time_stop - intent.ts).total_seconds())
    return cost_gate.evaluate(
        signal_edge_bps=edge_bps,
        side=intent.side.value,
        urgency=PARAMS.entry_urgency,
        expected_holding_seconds=holding_seconds,
        current_funding_rate=current_funding_rate,
        symbol=intent.symbol,
        available_room_bps=edge_bps,
    )


def _regime_permission(
    side: Side,
    context: StructureContext,
    *,
    symbol: str,
    as_of: datetime,
) -> tuple[bool, str, dict[str, str]]:
    """Apply supplied measurements as blockers and expose a score-free vector.

    Missing regime context is recorded but remains backward compatible.  Once a
    caller supplies a context, stale/future/bad-quality or opposing measurements
    fail closed.  A regime never upgrades an otherwise ineligible candidate.
    """
    diagnostics: dict[str, str] = {
        "diagnostics_policy": "measurement_vector_no_grade",
        "regime_1h": "not_supplied",
        "regime_4h": "not_supplied",
    }
    blocked_labels = {
        RegimeLabel.LOW_LIQUIDITY,
        RegimeLabel.MEAN_REVERSION,
        RegimeLabel.SIDEWAYS,
        RegimeLabel.UNAVAILABLE,
    }
    opposing = RegimeLabel.TRENDING_DOWN if side == Side.LONG else RegimeLabel.TRENDING_UP
    for expected_tf, regime in (("1h", context.regime_1h), ("4h", context.regime_4h)):
        if regime is None:
            continue
        prefix = f"regime_{expected_tf}"
        diagnostics[prefix] = regime.label.value
        diagnostics[f"{prefix}_confidence"] = f"{regime.confidence:.4f}"
        diagnostics[f"{prefix}_adx"] = "n/a" if regime.adx is None else f"{regime.adx:.4f}"
        diagnostics[f"{prefix}_atr_percentile"] = (
            "n/a" if regime.atr_percentile is None else f"{regime.atr_percentile:.4f}"
        )
        diagnostics[f"{prefix}_volume_ratio"] = (
            "n/a" if regime.volume_ratio is None else f"{regime.volume_ratio:.4f}"
        )
        if (
            not regime.ready
            or regime.data_quality != "ok"
            or regime.symbol != symbol
            or regime.timeframe != expected_tf
            or not isinstance(regime.as_of, datetime)
            or regime.as_of > as_of
        ):
            return False, f"invalid_{prefix}", diagnostics
        if regime.label in blocked_labels:
            return False, f"{prefix}_{regime.label.value}", diagnostics
        if regime.label == opposing:
            return False, f"{prefix}_opposes_{side.value}", diagnostics
    return True, "regime_context_clear", diagnostics


def evaluate_bos_intents(
    intents: Sequence[StructureBosIntent],
    cost_gate: CostGate,
    *,
    current_funding_rate: Decimal | float | str = Decimal(0),
) -> tuple[tuple[StructureBosIntent, CostGateResult], ...]:
    """Research-only bridge returning only CostGate survivors with reports."""
    survived: list[tuple[StructureBosIntent, CostGateResult]] = []
    for intent in intents:
        decision = evaluate_bos_intent(
            intent,
            cost_gate,
            current_funding_rate=current_funding_rate,
        )
        if decision.approved:
            survived.append((intent, decision))
    return tuple(survived)


_FEATURE_DEFAULTS: dict[str, object] = {
    "structure_ready": False,
    "structure_trend": StructureTrend.NONE.value,
    "structure_labels": "",
    "structure_event": StructureEventType.NONE.value,
    "last_swing_high": math.nan,
    "previous_swing_high": math.nan,
    "last_swing_low": math.nan,
    "previous_swing_low": math.nan,
    "swing_low_avwap": math.nan,
    "swing_high_avwap": math.nan,
    "dual_avwap_bias": "unavailable",
    "bos_atr": math.nan,
    "mtf_alignment": Alignment.BLOCKED.value,
    "mtf_reason": "missing_series",
    "htf_structure_trend": StructureTrend.NONE.value,
    "htf_structure_labels": "",
}


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(str(value)))
    except (TypeError, ValueError):
        return False


def _closed_candles(
    df: pd.DataFrame,
    expected_timeframe: Literal["1h", "4h"] = "1h",
    *,
    preserve_closed_flag: bool = False,
) -> tuple[list[Candle], list[bool]] | None:
    """Translate the canonical lake frame, failing closed on missing fields."""
    required = {
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trade_count",
        "data_quality",
    }
    if not required.issubset(df.columns):
        return None

    bars: list[Candle] = []
    eligible: list[bool] = []
    for row in df.itertuples(index=False):
        timestamp = pd.Timestamp(row.timestamp)
        if timestamp.tzinfo is None:
            return None
        timestamp = timestamp.tz_convert("UTC")
        prices = [row.open, row.high, row.low, row.close]
        if not all(_finite(value) and float(value) > 0 for value in prices):
            return None

        volume = row.volume
        quote_volume = row.quote_volume
        trade_count = row.trade_count
        if str(getattr(row, "timeframe", expected_timeframe)).strip().lower() != expected_timeframe:
            return None
        is_closed = bool(getattr(row, "is_closed", True))
        quality_ok = str(row.data_quality).strip().lower() == "ok" and is_closed
        exact_volume_ok = (
            _finite(volume)
            and _finite(quote_volume)
            and _finite(trade_count)
            and float(volume) >= 0
            and float(quote_volume) >= 0
            and int(float(trade_count)) == float(trade_count)
            and int(float(trade_count)) >= 0
            and (
                (float(volume) == 0 and float(quote_volume) == 0)
                or (float(volume) > 0 and float(quote_volume) > 0 and int(float(trade_count)) > 0)
            )
        )
        row_eligible = quality_ok and exact_volume_ok
        eligible.append(row_eligible)

        base = float(volume) if row_eligible else 0.0
        quote = float(quote_volume) if row_eligible else 0.0
        count = int(float(trade_count)) if row_eligible else 0
        taker_raw = getattr(row, "taker_buy_volume", 0.0)
        taker = float(taker_raw) if row_eligible and _finite(taker_raw) else 0.0
        if taker < 0 or taker > base:
            eligible[-1] = False
            base = quote = taker = 0.0
            count = 0

        try:
            bars.append(
                Candle(
                    symbol=str(getattr(row, "symbol", "BTCUSDT")),
                    timeframe=expected_timeframe,
                    open_time=timestamp.to_pydatetime(),
                    close_time=(
                        timestamp + pd.Timedelta(hours=4 if expected_timeframe == "4h" else 1)
                    ).to_pydatetime(),
                    open=Decimal(str(row.open)),
                    high=Decimal(str(row.high)),
                    low=Decimal(str(row.low)),
                    close=Decimal(str(row.close)),
                    volume=Decimal(str(base)),
                    quote_volume=Decimal(str(quote)),
                    trade_count=count,
                    taker_buy_volume=Decimal(str(taker)),
                    is_closed=is_closed if preserve_closed_flag else True,
                )
            )
        except ValueError:
            return None
    return bars, eligible


def _avwap(
    quote_prefix: list[Decimal],
    base_prefix: list[Decimal],
    anchor: SwingAnchor | None,
    index: int,
) -> Decimal | None:
    if anchor is None:
        return None
    return vwap_from_sums(
        quote_prefix[index + 1] - quote_prefix[anchor.index],
        base_prefix[index + 1] - base_prefix[anchor.index],
    )


def _set_feature(frame: pd.DataFrame, index: int, name: str, value: object) -> None:
    frame.iat[index, frame.columns.get_loc(name)] = value


def _add_structure_features(
    df: pd.DataFrame,
    config: SwingDetectConfig,
    p: StructureBosParams,
) -> pd.DataFrame:
    out = df.copy()
    for name, default in _FEATURE_DEFAULTS.items():
        out[name] = default

    if {"high", "low", "close"}.issubset(out.columns):
        previous = out["close"].shift(1)
        true_range = pd.concat(
            [
                out["high"] - out["low"],
                (out["high"] - previous).abs(),
                (out["low"] - previous).abs(),
            ],
            axis=1,
        ).max(axis=1)
        out["bos_atr"] = true_range.rolling(p.atr_period, min_periods=p.atr_period).mean()

    built = _closed_candles(out)
    if built is None:
        return out
    bars, eligible = built
    anchors = detect_swings(bars, config, eligible=eligible)
    quote_prefix = [Decimal(0)]
    base_prefix = [Decimal(0)]
    for bar in bars:
        quote_prefix.append(quote_prefix[-1] + bar.quote_volume)
        base_prefix.append(base_prefix[-1] + bar.volume)
    confirmations: dict[int, list[SwingAnchor]] = defaultdict(list)
    for anchor in anchors:
        confirmations[anchor.index + anchor.right].append(anchor)

    highs: deque[SwingAnchor] = deque(maxlen=2)
    lows: deque[SwingAnchor] = deque(maxlen=2)
    for index, usable in enumerate(eligible):
        if not usable:
            highs.clear()
            lows.clear()
            continue
        for anchor in confirmations.get(index, []):
            (lows if anchor.kind == SwingKind.LOW else highs).append(anchor)

        last_low = lows[-1] if lows else None
        last_high = highs[-1] if highs else None
        low_vwap = _avwap(quote_prefix, base_prefix, last_low, index)
        high_vwap = _avwap(quote_prefix, base_prefix, last_high, index)
        bias: DualAVWAPBias = dual_avwap_bias(bars[index].close, low_vwap, high_vwap)

        pair = SwingPairState(
            high_prev=highs[0] if len(highs) == 2 else None,
            high_last=last_high,
            low_prev=lows[0] if len(lows) == 2 else None,
            low_last=last_low,
        )
        trend = classify_hh_hl(pair)
        labels = structure_labels(pair)
        state = StructureState(
            as_of=bars[index].close_time,
            trend=trend,
            pair=pair,
            last_swing_high=last_high.anchor_price if last_high is not None else None,
            last_swing_low=last_low.anchor_price if last_low is not None else None,
            labels=labels,
        )
        event = detect_bos_choch(state, bars[index].close, p.break_buffer_bps)

        _set_feature(out, index, "structure_ready", trend != StructureTrend.NONE)
        _set_feature(out, index, "structure_trend", trend.value)
        _set_feature(out, index, "structure_labels", ",".join(labels))
        _set_feature(
            out,
            index,
            "structure_event",
            event.event.value if event is not None else StructureEventType.NONE.value,
        )
        if highs:
            _set_feature(out, index, "last_swing_high", float(highs[-1].anchor_price))
        if len(highs) == 2:
            _set_feature(out, index, "previous_swing_high", float(highs[0].anchor_price))
        if lows:
            _set_feature(out, index, "last_swing_low", float(lows[-1].anchor_price))
        if len(lows) == 2:
            _set_feature(out, index, "previous_swing_low", float(lows[0].anchor_price))
        if low_vwap is not None:
            _set_feature(out, index, "swing_low_avwap", float(low_vwap))
        if high_vwap is not None:
            _set_feature(out, index, "swing_high_avwap", float(high_vwap))
        _set_feature(out, index, "dual_avwap_bias", bias)
    return out


def _state_from_feature_row(row: pd.Series, as_of: datetime) -> StructureState:
    try:
        trend = StructureTrend(str(row["structure_trend"]))
    except ValueError:
        trend = StructureTrend.NONE
    labels = tuple(label for label in str(row["structure_labels"]).split(",") if label)
    high = Decimal(str(row["last_swing_high"])) if _finite(row["last_swing_high"]) else None
    low = Decimal(str(row["last_swing_low"])) if _finite(row["last_swing_low"]) else None
    return StructureState(
        as_of=as_of,
        trend=trend,
        pair=SwingPairState(None, None, None, None),
        last_swing_high=high,
        last_swing_low=low,
        labels=labels,
    )


def _event_from_feature_row(
    row: pd.Series,
    state: StructureState,
) -> StructureEvent | None:
    try:
        event_type = StructureEventType(str(row["structure_event"]))
    except ValueError:
        return None
    if event_type == StructureEventType.NONE:
        return None
    level = (
        state.last_swing_high
        if event_type in {StructureEventType.BOS_UP, StructureEventType.CHOCH_UP}
        else state.last_swing_low
    )
    return StructureEvent(
        event=event_type,
        ts=state.as_of,
        level=level,
        close=Decimal(str(row["close"])),
        prior_trend=state.trend,
    )


def _add_mtf_features(
    df: pd.DataFrame,
    htf_candles: pd.DataFrame | None,
    p: StructureBosParams,
) -> pd.DataFrame:
    out = df.copy()
    if htf_candles is None or htf_candles.empty:
        return out
    ltf_built = _closed_candles(out)
    htf_built = _closed_candles(
        htf_candles,
        "4h",
        preserve_closed_flag=True,
    )
    if ltf_built is None or htf_built is None:
        out["mtf_reason"] = "invalid_series"
        return out
    ltf_bars, ltf_eligible = ltf_built
    htf_bars, htf_eligible = htf_built
    if {bar.symbol for bar in htf_bars} != {bar.symbol for bar in ltf_bars}:
        out["mtf_reason"] = "symbol_mismatch"
        return out
    htf_config = SwingDetectConfig(
        left=p.mtf.htf_left,
        right=p.mtf.htf_right,
        strict=True,
    )

    for index, (ltf_bar, usable) in enumerate(zip(ltf_bars, ltf_eligible, strict=True)):
        if not usable:
            _set_feature(out, index, "mtf_reason", "invalid_ltf_quality")
            continue
        as_of = ltf_bar.close_time
        relevant = [position for position, bar in enumerate(htf_bars) if bar.close_time <= as_of]
        if any(not htf_eligible[position] for position in relevant):
            _set_feature(out, index, "mtf_reason", "invalid_htf_quality")
            continue
        visible_htf = fully_closed_htf(htf_bars, as_of)
        if not visible_htf:
            _set_feature(out, index, "mtf_reason", "no_closed_htf")
            continue
        try:
            htf_state = structure_from_bars(visible_htf, as_of, htf_config)
        except ValueError:
            _set_feature(out, index, "mtf_reason", "invalid_htf_series")
            continue

        row = out.iloc[index]
        ltf_state = _state_from_feature_row(row, as_of)
        ltf_event = _event_from_feature_row(row, ltf_state)
        alignment, reason = align_structure(htf_state, ltf_state, ltf_event, p.mtf)
        _set_feature(out, index, "mtf_alignment", alignment.value)
        _set_feature(out, index, "mtf_reason", reason)
        _set_feature(out, index, "htf_structure_trend", htf_state.trend.value)
        _set_feature(out, index, "htf_structure_labels", ",".join(htf_state.labels))
    return out


class StructureBos1H(BaseStrategy):
    """S1 engine with raw research and CostGate-filtered backtest APIs."""

    strategy_id = strategy_id
    eligibility = eligibility
    timeframe = timeframe
    params = PARAMS
    cost_profile = cost_profile
    # Bar index 49 is the 50th closed bar and is therefore the first eligible
    # decision boundary under ``min_bars=50``.
    warmup_bars = PARAMS.min_bars - 1

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        htf_candles: pd.DataFrame | None = None,
        params: StructureBosParams | None = None,
    ) -> None:
        selected = params or PARAMS
        if selected != PARAMS:
            raise ValueError("structure_bos_1h params are frozen; use a new strategy ID")
        self.params = selected
        self.funding = funding
        self.htf_candles = htf_candles
        self._swing_config = SwingDetectConfig(
            left=self.params.left,
            right=self.params.right,
            strict=self.params.strict_swings,
        )
        self._cost_gate = CostGate(
            self.cost_profile,
            min_net_edge_bps=self.params.min_net_edge_bps,
            min_room_cost_multiple=self.params.min_room_cost_multiple,
        )

    def on_closed_candle(
        self,
        symbol: str,
        bars_1h: Sequence[Candle],
        bars_4h: Sequence[Candle],
        ctx: StructureContext | None = None,
    ) -> list[StructureBosIntent]:
        """Emit only 4h-aligned, non-executable 1h BoS research artifacts."""
        context = ctx or StructureContext()
        snapshot = build_mtf_snapshot(
            bars_4h,
            bars_1h,
            htf_tf="4h",
            ltf_tf="1h",
            params=self.params.mtf,
            data_quality=context.data_quality,
        )
        expected_side = {
            Alignment.LONG: Side.LONG,
            Alignment.SHORT: Side.SHORT,
        }.get(snapshot.alignment)
        if expected_side is None:
            return []
        intents = self._ltf_closed_intents(symbol, bars_1h, context)
        aligned: list[StructureBosIntent] = []
        for intent in intents:
            if intent.side != expected_side:
                continue
            regime_ok, regime_reason, diagnostics = _regime_permission(
                intent.side,
                context,
                symbol=symbol,
                as_of=intent.ts,
            )
            if not regime_ok:
                continue
            aligned.append(
                replace(
                    intent,
                    meta=MappingProxyType(
                        {
                            **dict(intent.meta),
                            "mtf_alignment": snapshot.alignment.value,
                            "mtf_reason": snapshot.reason,
                            "htf_tf": snapshot.htf_tf,
                            "htf_trend": snapshot.htf.trend.value,
                            "htf_labels": ",".join(snapshot.htf.labels),
                            "regime_reason": regime_reason,
                            "min_room_cost_multiple": str(self.params.min_room_cost_multiple),
                            **diagnostics,
                        }
                    ),
                )
            )
        return aligned

    def _ltf_closed_intents(
        self,
        symbol: str,
        bars: Sequence[Candle],
        context: StructureContext,
    ) -> list[StructureBosIntent]:
        p = self.params
        if context.data_quality != "ok" or len(bars) < p.min_bars:
            return []
        if any(
            not bar.is_closed or bar.timeframe != timeframe or bar.symbol != symbol for bar in bars
        ):
            return []

        latest = bars[-1]
        previous = bars[-2]
        as_of = latest.close_time
        swings = detect_swings(bars, self._swing_config)
        state = build_structure_state(swings, as_of)
        event = detect_bos_choch(state, latest.close, p.break_buffer_bps)
        last_high = state.pair.high_last
        last_low = state.pair.low_last
        if event is None or last_high is None or last_low is None:
            return []

        if event.event == StructureEventType.BOS_UP:
            level = last_high.anchor_price
            break_price = _bps_offset(level, p.break_buffer_bps, 1)
            bias_ok = not (p.require_bias_not_against and context.dual_avwap_bias == "strong_short")
            if previous.close <= break_price < latest.close and bias_ok:
                stop = _long_stop(bars, latest.close, last_low.anchor_price, p)
                if 0 < stop < latest.close:
                    return [
                        _research_intent(
                            symbol=symbol,
                            side=Side.LONG,
                            as_of=as_of,
                            entry=latest.close,
                            stop=stop,
                            level=level,
                            opposite_swing=last_low.anchor_price,
                            bias=context.dual_avwap_bias,
                            trend=StructureTrend.UP.value,
                            p=p,
                        )
                    ]
        if event.event == StructureEventType.BOS_DOWN:
            level = last_low.anchor_price
            break_price = _bps_offset(level, p.break_buffer_bps, -1)
            bias_ok = not (p.require_bias_not_against and context.dual_avwap_bias == "strong_long")
            if previous.close >= break_price > latest.close and bias_ok:
                stop = _short_stop(bars, latest.close, last_high.anchor_price, p)
                if stop > latest.close:
                    return [
                        _research_intent(
                            symbol=symbol,
                            side=Side.SHORT,
                            as_of=as_of,
                            entry=latest.close,
                            stop=stop,
                            level=level,
                            opposite_swing=last_high.anchor_price,
                            bias=context.dual_avwap_bias,
                            trend=StructureTrend.DOWN.value,
                            p=p,
                        )
                    ]
        return []

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        funded = merge_funding(candles, self.funding)
        structured = _add_structure_features(funded, self._swing_config, self.params)
        return _add_mtf_features(structured, self.htf_candles, self.params)

    def _candidate(self, df: pd.DataFrame, index: int) -> BacktestSignalIntent | None:
        if index <= 0 or index >= len(df) or index + 1 < self.params.min_bars:
            return None
        row = df.iloc[index]
        previous = df.iloc[index - 1]
        if not bool(row["structure_ready"]):
            return None
        required = (
            "close",
            "last_swing_high",
            "last_swing_low",
            "funding_rate",
        )
        if not all(_finite(row[name]) for name in required):
            return None

        close = float(row["close"])
        prior_close = float(previous["close"])
        last_high = float(row["last_swing_high"])
        last_low = float(row["last_swing_low"])
        bias = str(row["dual_avwap_bias"])
        event = str(row["structure_event"])
        alignment = str(row["mtf_alignment"])
        break_buffer = float(self.params.break_buffer_bps) / 10_000
        stop_buffer = float(self.params.stop_buffer_bps) / 10_000
        atr = float(row["bos_atr"]) if _finite(row["bos_atr"]) else None
        reward_r = float(self.params.cost_edge_reward_r)

        if (
            event == StructureEventType.BOS_UP.value
            and alignment == Alignment.LONG.value
            and prior_close <= last_high * (1 + break_buffer) < close
            and not (self.params.require_bias_not_against and bias == "strong_short")
        ):
            stop = last_low * (1 - stop_buffer)
            if self.params.use_atr_stop_cap and atr is not None and atr > 0:
                stop = max(stop, close - float(self.params.atr_stop_mult) * atr)
            risk = close - stop
            if risk <= 0:
                return None
            return BacktestSignalIntent(
                side="long",
                stop_price=stop,
                take_profit_price=close + reward_r * risk,
                reason="bos_up_break_swing_high",
            )
        if (
            event == StructureEventType.BOS_DOWN.value
            and alignment == Alignment.SHORT.value
            and prior_close >= last_low * (1 - break_buffer) > close
            and not (self.params.require_bias_not_against and bias == "strong_long")
        ):
            stop = last_high * (1 + stop_buffer)
            if self.params.use_atr_stop_cap and atr is not None and atr > 0:
                stop = min(stop, close + float(self.params.atr_stop_mult) * atr)
            risk = stop - close
            target = close - reward_r * risk
            if risk <= 0 or target <= 0:
                return None
            return BacktestSignalIntent(
                side="short",
                stop_price=stop,
                take_profit_price=target,
                reason="bos_down_break_swing_low",
            )
        return None

    def _research_from_frame(
        self,
        df: pd.DataFrame,
        index: int,
        candidate: BacktestSignalIntent,
    ) -> StructureBosIntent:
        row = df.iloc[index]
        opened = pd.Timestamp(row["timestamp"])
        if opened.tzinfo is None:
            raise ValueError("structure_bos_1h timestamps must be timezone-aware")
        as_of = (opened.tz_convert("UTC") + pd.Timedelta(hours=1)).to_pydatetime()
        side = Side(candidate.side)
        is_long = side == Side.LONG
        bias = str(row["dual_avwap_bias"])
        if bias not in {"strong_long", "strong_short", "between", "n/a", "unavailable"}:
            bias = "n/a"
        return _research_intent(
            symbol=str(row.get("symbol", "BTCUSDT")),
            side=side,
            as_of=as_of,
            entry=Decimal(str(row["close"])),
            stop=Decimal(str(candidate.stop_price)),
            level=Decimal(str(row["last_swing_high" if is_long else "last_swing_low"])),
            opposite_swing=Decimal(str(row["last_swing_low" if is_long else "last_swing_high"])),
            bias=bias,  # type: ignore[arg-type]
            trend="up" if is_long else "down",
            p=self.params,
        )

    def evaluate(self, df: pd.DataFrame, index: int) -> StructureBosEvaluation:
        candidate = self._candidate(df, index)
        if candidate is None:
            return StructureBosEvaluation(None, None, None, False, "no causal BoS candidate")
        research = self._research_from_frame(df, index, candidate)
        result = evaluate_bos_intent(
            research,
            self._cost_gate,
            current_funding_rate=df.iloc[index]["funding_rate"],
        )
        if not result.approved:
            return StructureBosEvaluation(
                candidate,
                research,
                result,
                False,
                result.reason or "CostGate rejected",
            )
        return StructureBosEvaluation(
            candidate,
            research,
            result,
            True,
            (
                f"CostGate approved: cost={result.cost.total_cost_bps:.2f}bps, "
                f"net={result.expected_net_bps:.2f}bps"
            ),
        )

    def signal(self, df: pd.DataFrame, index: int) -> BacktestSignalIntent | None:
        evaluation = self.evaluate(df, index)
        if not evaluation.accepted or evaluation.candidate is None:
            return None
        intent = evaluation.candidate
        signal_id_value = (
            evaluation.research_intent.signal_id if evaluation.research_intent else "missing"
        )
        return BacktestSignalIntent(
            side=intent.side,
            stop_price=intent.stop_price,
            take_profit_price=intent.take_profit_price,
            reason=f"{intent.reason}; signal_id={signal_id_value}; {evaluation.reason}",
        )


StructureBos1h = StructureBos1H
StructureBos1HParams = StructureBosParams


__all__ = [
    "PARAMS",
    "STRATEGY_SPEC",
    "Side",
    "StructureBos1H",
    "StructureBos1HParams",
    "StructureBos1h",
    "StructureBosEvaluation",
    "StructureBosIntent",
    "StructureBosParams",
    "StructureContext",
    "cost_profile",
    "eligibility",
    "evaluate_bos_intent",
    "evaluate_bos_intents",
    "params",
    "strategy_id",
    "timeframe",
]
