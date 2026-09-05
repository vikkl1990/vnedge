"""Causal hidden MACD divergence experiment on closed 15-minute bars.

This module deliberately registers one claim only: hidden divergence as a
continuation setup.  Regular divergence is classified by the shared helper so
the four inequalities remain explicit and testable, but it cannot emit an
intent from this strategy ID.

Frozen contract
---------------
* price pivots: ``detect_swings(left=3, right=3, strict=True)``;
* oscillator: MACD line, never pivots of the MACD line;
* EMA: pandas ``ewm(span=..., adjust=False, min_periods=span)``;
* fire: the close that confirms the second price pivot;
* entry: next 15-minute open;
* stop: the second price-pivot invalidation; target: 2R;
* authority: research only, with no HTF regime or ``macd_impulse`` gate.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pandas as pd  # type: ignore[import-untyped]

from vnedge.data.candles import Candle
from vnedge.data.swings import SwingAnchor, SwingDetectConfig, SwingKind, detect_swings
from vnedge.strategy.arm_evidence import FrozenPermissionSnapshot, freeze_permission_from_row
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

DivergenceKind = Literal[
    "regular_bull",
    "hidden_bull",
    "regular_bear",
    "hidden_bear",
]


@dataclass(frozen=True, slots=True)
class MacdSpec:
    fast_span: int = 12
    slow_span: int = 26
    signal_span: int = 9
    adjust: bool = False
    min_periods_mode: Literal["span"] = "span"
    compare_series: Literal["macd_line"] = "macd_line"

    def __post_init__(self) -> None:
        if min(self.fast_span, self.slow_span, self.signal_span) < 1:
            raise ValueError("MACD spans must be positive")
        if self.fast_span >= self.slow_span:
            raise ValueError("MACD fast span must be below slow span")
        if self.adjust:
            raise ValueError("this strategy freezes MACD adjust=False")

    @property
    def identity(self) -> str:
        return (
            f"ema_{self.fast_span}_{self.slow_span}_signal_{self.signal_span}"
            f"_adjust_false_min_periods_span_compare_{self.compare_series}"
        )


@dataclass(frozen=True, slots=True)
class MacdHiddenDivergenceParams:
    swing_left: int = 3
    swing_right: int = 3
    swing_strict: bool = True
    reward_r: float = 2.0
    macd: MacdSpec = MacdSpec()

    def __post_init__(self) -> None:
        if self.swing_left < 1 or self.swing_right < 1:
            raise ValueError("swing windows must be positive")
        if not self.swing_strict:
            raise ValueError("this strategy freezes strict price swings")
        if self.reward_r <= 0:
            raise ValueError("reward_r must be positive")


@dataclass(frozen=True, slots=True)
class DivergenceAnchorEvidence:
    swing_id: str
    kind: Literal["swing_low", "swing_high"]
    open_time: datetime
    confirmed_at: datetime
    price: float
    macd_line: float

    def as_dict(self) -> dict[str, object]:
        return {
            "swing_id": self.swing_id,
            "kind": self.kind,
            "open_time": self.open_time.isoformat(),
            "confirmed_at": self.confirmed_at.isoformat(),
            "price": self.price,
            "macd_line": self.macd_line,
        }


@dataclass(frozen=True, slots=True)
class MacdDivergenceEvidence:
    strategy_id: str
    side: Literal["long", "short"]
    divergence_kind: Literal["hidden_bull", "hidden_bear"]
    first: DivergenceAnchorEvidence
    second: DivergenceAnchorEvidence
    macd_spec: str
    episode_id: str
    decision_open: datetime
    entry_clock: Literal["next_15m_open"]
    snapshot_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "side": self.side,
            "divergence_kind": self.divergence_kind,
            "first": self.first.as_dict(),
            "second": self.second.as_dict(),
            "macd_spec": self.macd_spec,
            "episode_id": self.episode_id,
            "decision_open": self.decision_open.isoformat(),
            "entry_clock": self.entry_clock,
            "snapshot_id": self.snapshot_id,
        }


@dataclass(frozen=True)
class MacdDivergenceSignalIntent(SignalIntent):
    """Research intent carrying pattern proof without changing OrderIntent."""

    divergence_evidence: MacdDivergenceEvidence | None = None


def classify_price_oscillator_divergence(
    *,
    swing_kind: SwingKind,
    first_price: float,
    second_price: float,
    first_oscillator: float,
    second_oscillator: float,
) -> DivergenceKind | None:
    """Classify the four strict price/oscillator divergence inequalities."""

    values = (first_price, second_price, first_oscillator, second_oscillator)
    if not all(math.isfinite(value) for value in values):
        return None
    if swing_kind is SwingKind.LOW:
        if second_price < first_price and second_oscillator > first_oscillator:
            return "regular_bull"
        if second_price > first_price and second_oscillator < first_oscillator:
            return "hidden_bull"
        return None
    if second_price > first_price and second_oscillator < first_oscillator:
        return "regular_bear"
    if second_price < first_price and second_oscillator > first_oscillator:
        return "hidden_bear"
    return None


def macd_frame(close: pd.Series, spec: MacdSpec | None = None) -> pd.DataFrame:
    """Return the frozen MACD line/signal/histogram series.

    ``min_periods`` is applied independently to each EMA.  Consequently the
    line first appears after ``slow_span`` valid closes and the signal/histogram
    first appears after ``signal_span`` valid MACD-line observations.
    """

    spec = MacdSpec() if spec is None else spec
    numeric = pd.to_numeric(close, errors="coerce").astype(float)
    fast = numeric.ewm(
        span=spec.fast_span,
        adjust=False,
        min_periods=spec.fast_span,
    ).mean()
    slow = numeric.ewm(
        span=spec.slow_span,
        adjust=False,
        min_periods=spec.slow_span,
    ).mean()
    line = fast - slow
    signal = line.ewm(
        span=spec.signal_span,
        adjust=False,
        min_periods=spec.signal_span,
    ).mean()
    return pd.DataFrame(
        {
            "mhd_macd_line": line,
            "mhd_macd_signal": signal,
            "mhd_macd_hist": line - signal,
        },
        index=close.index,
    )


def _utc(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError("MACD divergence timestamps must be timezone-aware")
    return timestamp.tz_convert("UTC").to_pydatetime().astimezone(UTC)


def _decimal(value: object, *, field: str) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"MACD divergence {field} must be finite")
    return result


def _source_identity(frame: pd.DataFrame) -> tuple[str, str]:
    exchange = str(frame.attrs.get("exchange", "unreported"))
    symbol = str(frame.attrs.get("symbol", "RESEARCH"))
    if "exchange" in frame.columns and len(frame):
        exchange = str(frame["exchange"].iloc[-1])
    if "symbol" in frame.columns and len(frame):
        symbol = str(frame["symbol"].iloc[-1])
    return exchange, symbol


def _candles_and_eligibility(frame: pd.DataFrame) -> tuple[list[Candle], list[bool]]:
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"macd_hidden_div_15m_v1 missing columns: {sorted(missing)}")
    if "timeframe" in frame.columns:
        declared = {str(value) for value in frame["timeframe"].dropna().unique()}
        if declared and declared != {"15m"}:
            raise ValueError("macd_hidden_div_15m_v1 requires 15m candles")
    _, symbol = _source_identity(frame)
    quality = (
        frame["data_quality"].astype(str).str.lower().eq("ok")
        if "data_quality" in frame.columns
        else pd.Series(True, index=frame.index)
    )
    closed = (
        frame["is_closed"].eq(True).fillna(False).astype(bool)
        if "is_closed" in frame.columns
        else pd.Series(True, index=frame.index)
    )
    candles: list[Candle] = []
    for position in range(len(frame)):
        row = frame.iloc[position]
        opened = _utc(row["timestamp"])
        volume = _decimal(row.get("volume", 0), field="volume")
        quote_volume = _decimal(row.get("quote_volume", 0), field="quote_volume")
        # Legacy research frames predate the canonical trade_count column.
        # The synthetic fallback only satisfies Candle's structural invariant;
        # trade count is not a feature or gate in this price-only strategy.
        trade_count = int(row.get("trade_count", 1 if volume > 0 else 0))
        candles.append(
            Candle(
                symbol=symbol,
                timeframe="15m",
                open_time=opened,
                close_time=opened + timedelta(minutes=15),
                open=_decimal(row["open"], field="open"),
                high=_decimal(row["high"], field="high"),
                low=_decimal(row["low"], field="low"),
                close=_decimal(row["close"], field="close"),
                volume=volume,
                quote_volume=quote_volume,
                trade_count=trade_count,
                is_closed=bool(closed.iloc[position]),
            )
        )
    return candles, (quality & closed).tolist()


def _swing_id(
    anchor: SwingAnchor,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
) -> str:
    payload = {
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "kind": anchor.kind.value,
        "open_time": anchor.anchor_time.isoformat(),
        "confirmed_at": anchor.confirmed_at.isoformat(),
        "price": format(anchor.anchor_price.normalize(), "f"),
        "left": anchor.left,
        "right": anchor.right,
        "strict": anchor.strict,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]


def _episode_id(
    *,
    strategy_id: str,
    side: str,
    first_swing_id: str,
    second_swing_id: str,
    macd_spec: str,
) -> str:
    payload = {
        "strategy_id": strategy_id,
        "side": side,
        "swing1_id": first_swing_id,
        "swing2_id": second_swing_id,
        "macd_spec": macd_spec,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]


class MacdHiddenDivergence15mV1(BaseStrategy):
    strategy_id = "macd_hidden_div_15m_v1"
    eligibility = "RESEARCH_ONLY"
    timeframe = "15m"
    params = MacdHiddenDivergenceParams()
    # Histogram needs 26 valid closes plus 9 valid MACD observations.  The
    # strategy compares the line, but keeps the complete frozen MACD family.
    warmup_bars = 34

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = candles.copy()
        candle_rows, eligible = _candles_and_eligibility(out)
        macd = macd_frame(out["close"], self.params.macd)
        for column in macd.columns:
            out[column] = macd[column]
        out["mhd_quality_ok"] = pd.Series(eligible, index=out.index).astype(float)
        out["mhd_pattern"] = "none"
        out["mhd_fire"] = 0.0
        out["mhd_side"] = "none"
        out["mhd_episode_id"] = None
        out["mhd_evidence"] = None
        out["mhd_first_swing_open"] = pd.Series(
            pd.NaT,
            index=out.index,
            dtype="datetime64[ns, UTC]",
        )
        out["mhd_second_swing_open"] = pd.Series(
            pd.NaT,
            index=out.index,
            dtype="datetime64[ns, UTC]",
        )

        config = SwingDetectConfig(
            left=self.params.swing_left,
            right=self.params.swing_right,
            strict=self.params.swing_strict,
        )
        anchors = detect_swings(candle_rows, config, eligible=eligible)
        exchange, symbol = _source_identity(out)
        last_by_kind: dict[SwingKind, SwingAnchor] = {}
        candidates_by_confirmation: dict[int, list[MacdDivergenceEvidence]] = {}

        for anchor in anchors:
            prior = last_by_kind.get(anchor.kind)
            last_by_kind[anchor.kind] = anchor
            if prior is None:
                continue
            first_macd = float(out["mhd_macd_line"].iloc[prior.index])
            second_macd = float(out["mhd_macd_line"].iloc[anchor.index])
            kind = classify_price_oscillator_divergence(
                swing_kind=anchor.kind,
                first_price=float(prior.anchor_price),
                second_price=float(anchor.anchor_price),
                first_oscillator=first_macd,
                second_oscillator=second_macd,
            )
            confirmation_index = anchor.index + anchor.right
            out.iloc[confirmation_index, out.columns.get_loc("mhd_first_swing_open")] = (
                pd.Timestamp(prior.anchor_time)
            )
            out.iloc[confirmation_index, out.columns.get_loc("mhd_second_swing_open")] = (
                pd.Timestamp(anchor.anchor_time)
            )
            if kind is None:
                continue
            out.iloc[confirmation_index, out.columns.get_loc("mhd_pattern")] = kind
            if kind not in {"hidden_bull", "hidden_bear"}:
                continue
            side: Literal["long", "short"] = "long" if kind == "hidden_bull" else "short"
            first_id = _swing_id(
                prior,
                exchange=exchange,
                symbol=symbol,
                timeframe=self.timeframe,
            )
            second_id = _swing_id(
                anchor,
                exchange=exchange,
                symbol=symbol,
                timeframe=self.timeframe,
            )
            row = out.iloc[confirmation_index]
            permission = freeze_permission_from_row(
                row.to_dict(),
                decision_timeframe=self.timeframe,
                context_timeframes=(),
                allow_long=side == "long",
                allow_short=side == "short",
                reason=self.strategy_id,
                regime_version=self.strategy_id,
            )
            episode = _episode_id(
                strategy_id=self.strategy_id,
                side=side,
                first_swing_id=first_id,
                second_swing_id=second_id,
                macd_spec=self.params.macd.identity,
            )
            evidence = MacdDivergenceEvidence(
                strategy_id=self.strategy_id,
                side=side,
                divergence_kind=kind,
                first=DivergenceAnchorEvidence(
                    swing_id=first_id,
                    kind=prior.kind.value,
                    open_time=prior.anchor_time,
                    confirmed_at=prior.confirmed_at,
                    price=float(prior.anchor_price),
                    macd_line=first_macd,
                ),
                second=DivergenceAnchorEvidence(
                    swing_id=second_id,
                    kind=anchor.kind.value,
                    open_time=anchor.anchor_time,
                    confirmed_at=anchor.confirmed_at,
                    price=float(anchor.anchor_price),
                    macd_line=second_macd,
                ),
                macd_spec=self.params.macd.identity,
                episode_id=episode,
                decision_open=_utc(row["timestamp"]),
                entry_clock="next_15m_open",
                snapshot_id=permission.snapshot_id,
            )
            # Permission is regenerated in signal() from the same immutable row;
            # evidence keeps only its deterministic snapshot id in the frame.
            candidates_by_confirmation.setdefault(confirmation_index, []).append(evidence)

        for position, candidates in candidates_by_confirmation.items():
            # An outside bar can confirm a high and low together.  Two opposing
            # continuation claims are an ambiguous episode and must not fire.
            if len(candidates) != 1:
                out.iloc[position, out.columns.get_loc("mhd_pattern")] = "conflict"
                continue
            evidence = candidates[0]
            out.iloc[position, out.columns.get_loc("mhd_pattern")] = (
                evidence.divergence_kind
            )
            out.iloc[position, out.columns.get_loc("mhd_fire")] = 1.0
            out.iloc[position, out.columns.get_loc("mhd_side")] = evidence.side
            out.iloc[position, out.columns.get_loc("mhd_episode_id")] = evidence.episode_id
            out.iloc[position, out.columns.get_loc("mhd_evidence")] = evidence
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars - 1:
            return None
        row = df.iloc[index]
        if float(row.get("mhd_quality_ok", 0.0)) <= 0 or float(row.get("mhd_fire", 0.0)) <= 0:
            return None
        evidence = row.get("mhd_evidence")
        if not isinstance(evidence, MacdDivergenceEvidence):
            return None
        close = float(row["close"])
        stop = evidence.second.price
        if evidence.side == "long":
            risk = close - stop
            target = close + self.params.reward_r * risk
        else:
            risk = stop - close
            target = close - self.params.reward_r * risk
        if not all(math.isfinite(value) for value in (close, stop, risk, target)):
            return None
        if min(stop, risk, target) <= 0:
            return None
        permission: FrozenPermissionSnapshot = freeze_permission_from_row(
            row.to_dict(),
            decision_timeframe=self.timeframe,
            context_timeframes=(),
            allow_long=evidence.side == "long",
            allow_short=evidence.side == "short",
            reason=self.strategy_id,
            regime_version=self.strategy_id,
        )
        if permission.snapshot_id != evidence.snapshot_id:
            return None
        return MacdDivergenceSignalIntent(
            side=evidence.side,
            stop_price=stop,
            take_profit_price=target,
            reason=(
                f"{self.strategy_id} {evidence.divergence_kind} "
                f"episode={evidence.episode_id} next_15m_open research_only"
            ),
            permission_snapshot=permission,
            divergence_evidence=evidence,
        )

    def evaluation_diagnostics(self, df: pd.DataFrame, index: int) -> dict[str, Any]:
        row = df.iloc[index]
        failures: list[str] = []
        if float(row.get("mhd_quality_ok", 0.0)) <= 0:
            failures.append("data_quality_not_ok")
        if not math.isfinite(float(row.get("mhd_macd_line", math.nan))):
            failures.append("macd_line_not_ready")
        pattern = str(row.get("mhd_pattern", "none"))
        if pattern == "conflict":
            failures.append("opposing_hidden_divergence_conflict")
        elif pattern not in {"hidden_bull", "hidden_bear"}:
            failures.append("hidden_divergence_not_confirmed")
        if float(row.get("mhd_fire", 0.0)) <= 0:
            failures.append("no_hidden_divergence_setup")
        return {
            "eligible": not failures,
            "primary_failed_gate": failures[0] if failures else None,
            "all_failed_gates": failures,
            "features": {
                "mhd_quality_ok": float(row.get("mhd_quality_ok", 0.0)),
                "mhd_macd_line": (
                    float(row["mhd_macd_line"])
                    if math.isfinite(float(row.get("mhd_macd_line", math.nan)))
                    else None
                ),
                "mhd_macd_signal": (
                    float(row["mhd_macd_signal"])
                    if math.isfinite(float(row.get("mhd_macd_signal", math.nan)))
                    else None
                ),
                "mhd_macd_hist": (
                    float(row["mhd_macd_hist"])
                    if math.isfinite(float(row.get("mhd_macd_hist", math.nan)))
                    else None
                ),
                "mhd_pattern": pattern,
                "mhd_episode_id": row.get("mhd_episode_id"),
            },
            "thresholds": {
                "swing_left": float(self.params.swing_left),
                "swing_right": float(self.params.swing_right),
                "reward_r": self.params.reward_r,
            },
            "distance_to_threshold": {},
        }


__all__ = [
    "DivergenceAnchorEvidence",
    "DivergenceKind",
    "MacdDivergenceEvidence",
    "MacdDivergenceSignalIntent",
    "MacdHiddenDivergence15mV1",
    "MacdHiddenDivergenceParams",
    "MacdSpec",
    "classify_price_oscillator_divergence",
    "macd_frame",
]
