"""Read-only hour-by-hour market pulse projections.

This module turns canonical closed 1h candles and explicit integrity gaps into
small dashboard payloads. It cannot emit signals, intents, orders, or capital
permission. Narrative generators receive only the frozen numeric context and
their output is cached per exchange/symbol/hour.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.data.gaps import GapParquetStore, GapRecord
from vnedge.data.swings import SwingAnchor, SwingDetectConfig, SwingKind, detect_swings
from vnedge.data.vwap import dual_avwap_bias
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY

logger = logging.getLogger(__name__)

BRIEF_SCHEMA_VERSION = "1.0"
OBSERVATION_DISCLAIMER = (
    "Observation only. Not financial advice. No order permission."
)
_QUALITY_VALUES = frozenset({"ok", "degraded", "gap"})
_STATE_LABELS = frozenset(
    {
        "quiet",
        "range",
        "expansion",
        "trend_continuation",
        "reversal_attempt",
        "degraded_data",
    }
)
_BANNED_LANGUAGE = re.compile(
    r"\b(?:buy|sell|long|short|enter|exit|target|stop|leverage|guarantee(?:d|s)?)\b",
    re.IGNORECASE,
)
PULSE_1H_SWING_CONFIG = SwingDetectConfig(left=3, right=3, strict=True)


def _utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _number(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def _bps(numerator: Decimal, denominator: Decimal) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator / denominator * Decimal(10_000))


def _iso(value: datetime) -> str:
    return _utc(value, label="timestamp").isoformat().replace("+00:00", "Z")


def _gap_minutes(candle: Candle, gaps: Sequence[GapRecord]) -> float:
    """Union overlapping gap intervals so one minute is never double-counted."""
    intervals = sorted(
        (
            max(candle.open_time, gap.start),
            min(candle.close_time, gap.end),
        )
        for gap in gaps
        if gap.start < candle.close_time and gap.end > candle.open_time
    )
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return round(sum((end - start).total_seconds() for start, end in merged) / 60.0, 2)


def _session_label(hour: int) -> str:
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 12:
        return "europe"
    if 12 <= hour < 17:
        return "us_overlap"
    if 17 <= hour < 22:
        return "us"
    return "off_session"


@dataclass(frozen=True, slots=True)
class PulseHour:
    symbol: str
    open_time: str
    close_time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None
    session_vwap: float | None
    range_bps: float
    body_bps: float
    close_vs_open_bps: float
    volume_vs_median_20h: float | None
    volume_vs_median_24h: float | None
    volume_rank_24h: float | None
    vs_session_vwap_bps: float | None
    prior_hour_range_bps: float | None
    dual_avwap_bias: str
    avwap_low: float | None
    avwap_high: float | None
    avwap_low_anchor_utc: str | None
    avwap_high_anchor_utc: str | None
    avwap_low_confirmed_at_utc: str | None
    avwap_high_confirmed_at_utc: str | None
    session_active: bool
    session_label: str
    data_quality: str
    gap_minutes: float
    stream_healthy: bool
    forming: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Stable Pulse API aliases. Keep the original keys for existing clients
        # while making UTC and integrity semantics explicit for new consumers.
        payload["open_time_utc"] = self.open_time
        payload["close_time_utc"] = self.close_time
        payload["is_gap"] = self.data_quality == "gap"
        if payload["dual_avwap_bias"] == "unavailable":
            payload["dual_avwap_bias"] = "n/a"
        return payload

    def analysis_inputs(self, context_hours: Sequence[PulseHour]) -> dict[str, Any]:
        """Fixed server-owned JSON supplied to a narrative adapter."""
        return {
            "ohlc": {
                "open": round(self.open, 8),
                "high": round(self.high, 8),
                "low": round(self.low, 8),
                "close": round(self.close, 8),
                "range_bps": round(self.range_bps, 2),
                "body_bps": round(self.body_bps, 2),
                "close_vs_open_bps": round(self.close_vs_open_bps, 2),
            },
            "volume": {
                "volume": round(self.volume, 8),
                "rank_24h": (
                    round(self.volume_rank_24h, 4)
                    if self.volume_rank_24h is not None
                    else None
                ),
                "vs_median_24h": (
                    round(self.volume_vs_median_24h, 4)
                    if self.volume_vs_median_24h is not None
                    else None
                ),
            },
            "vwap": {
                "session_vwap": (
                    round(self.session_vwap, 8) if self.session_vwap is not None else None
                ),
                "vs_session_vwap_bps": (
                    round(self.vs_session_vwap_bps, 2)
                    if self.vs_session_vwap_bps is not None
                    else None
                ),
                "bar_vwap": round(self.vwap, 8) if self.vwap is not None else None,
            },
            "structure": {
                "prior_hour_range_bps": (
                    round(self.prior_hour_range_bps, 2)
                    if self.prior_hour_range_bps is not None
                    else None
                ),
                "dual_avwap_bias": self.dual_avwap_bias,
                "session_active": self.session_active,
                "session_label": self.session_label,
            },
            "quality": {
                "data_quality": self.data_quality,
                "gap_minutes": self.gap_minutes,
                "stream_healthy": self.stream_healthy,
            },
            "context_hours": [
                {
                    "open_utc": row.open_time,
                    "range_bps": round(row.range_bps, 2),
                    "close_vs_open_bps": round(row.close_vs_open_bps, 2),
                    "vol_rank": (
                        round(row.volume_rank_24h, 4)
                        if row.volume_rank_24h is not None
                        else None
                    ),
                }
                for row in context_hours[-6:]
            ],
        }


@dataclass(frozen=True, slots=True)
class HourBrief:
    schema_version: str
    brief_id: str
    exchange: str
    symbol: str
    hour_open_utc: str
    hour_close_utc: str
    generated_at_utc: str
    model: str
    data_quality: str
    inputs: dict[str, Any]
    sections: dict[str, Any]
    flags: dict[str, bool]
    disclaimer: str = OBSERVATION_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


BriefAnalyzer = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def _numeric_values(value: Any) -> list[float]:
    if isinstance(value, Mapping):
        return [number for child in value.values() for number in _numeric_values(child)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [number for child in value for number in _numeric_values(child)]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    return []


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _texts(sections: Mapping[str, Any]) -> list[str]:
    state = _mapping(sections.get("state"))
    mattered = _mapping(sections.get("what_mattered"))
    structure = _mapping(sections.get("structure"))
    risks = _mapping(sections.get("risks"))
    watch = _mapping(sections.get("watch_next"))
    bullets = mattered.get("bullets")
    risk_bullets = risks.get("bullets")
    bullet_rows = bullets if isinstance(bullets, list) else []
    risk_rows = risk_bullets if isinstance(risk_bullets, list) else []
    return [
        str(state.get("summary") or ""),
        *[str(item) for item in bullet_rows],
        str(structure.get("summary") or ""),
        *[str(item) for item in risk_rows],
        str(watch.get("summary") or ""),
    ]


def _percentile_75(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int(0.75 * (len(ordered) - 1))]


def _derive_flags(inputs: Mapping[str, Any]) -> dict[str, bool]:
    quality = _mapping(inputs.get("quality"))
    volume = _mapping(inputs.get("volume"))
    ohlc = _mapping(inputs.get("ohlc"))
    vwap = _mapping(inputs.get("vwap"))
    context = inputs.get("context_hours")
    context_rows = context if isinstance(context, list) else []
    prior_ranges: list[float] = []
    for item in context_rows:
        range_value = _mapping(item).get("range_bps")
        if range_value is not None:
            prior_ranges.append(float(range_value))
    threshold = _percentile_75(prior_ranges)
    current_range = float(ohlc.get("range_bps") or 0.0)
    rank = volume.get("rank_24h")
    distance = vwap.get("vs_session_vwap_bps")
    return {
        "feed_degraded": quality.get("data_quality") != "ok",
        "high_volume": rank is not None and float(rank) > 0.7,
        "wide_range": threshold is not None and current_range > threshold,
        "above_vwap": distance is not None and float(distance) > 0,
    }


def _sections_are_safe(
    sections: Mapping[str, Any], inputs: Mapping[str, Any]
) -> bool:
    if list(sections) != ["state", "what_mattered", "structure", "risks", "watch_next"]:
        return False
    state = _mapping(sections.get("state"))
    mattered = _mapping(sections.get("what_mattered"))
    structure = _mapping(sections.get("structure"))
    risks = _mapping(sections.get("risks"))
    watch = _mapping(sections.get("watch_next"))
    label = str(state.get("label") or "")
    state_summary = str(state.get("summary") or "")
    structure_summary = str(structure.get("summary") or "")
    watch_summary = str(watch.get("summary") or "")
    bullets = mattered.get("bullets")
    risk_bullets = risks.get("bullets")
    if (
        label not in _STATE_LABELS
        or len(label) > 24
        or not state_summary
        or len(state_summary) > 160
        or not structure_summary
        or len(structure_summary) > 200
        or not watch_summary
        or len(watch_summary) > 160
        or not isinstance(bullets, list)
        or not 2 <= len(bullets) <= 4
        or any(not isinstance(item, str) or not item or len(item) > 100 for item in bullets)
        or not isinstance(risk_bullets, list)
        or not 1 <= len(risk_bullets) <= 3
        or any(not isinstance(item, str) or not item or len(item) > 140 for item in risk_bullets)
    ):
        return False
    input_structure = _mapping(inputs.get("structure"))
    if structure.get("bias_tag") != input_structure.get("dual_avwap_bias"):
        return False
    quality = str(_mapping(inputs.get("quality")).get("data_quality") or "degraded")
    if quality != "ok":
        if label != "degraded_data":
            return False
        first_risk = str(risk_bullets[0]).lower()
        if "feed" not in first_risk and "gap" not in first_risk:
            return False
    prose = " ".join(_texts(sections))
    if _BANNED_LANGUAGE.search(prose):
        return False

    # A narrative may restate observed metrics but cannot invent a level.
    allowed = {24.0}
    for value in _numeric_values(inputs):
        for candidate in (value, abs(value), value * 100, abs(value) * 100):
            allowed.update(round(candidate, digits) for digits in (0, 1, 2, 4, 8))
    for token in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", prose):
        if not any(abs(float(token) - candidate) < 1e-6 for candidate in allowed):
            return False
    return True


def bounded_observation_brief(context: Mapping[str, Any]) -> Mapping[str, Any]:
    """Deterministic, schema-valid fallback when the configured model is offline."""
    inputs = _mapping(context.get("inputs"))
    ohlc = _mapping(inputs.get("ohlc"))
    volume = _mapping(inputs.get("volume"))
    vwap = _mapping(inputs.get("vwap"))
    structure_input = _mapping(inputs.get("structure"))
    quality_input = _mapping(inputs.get("quality"))
    range_bps = float(ohlc.get("range_bps") or 0.0)
    close_vs_open = float(ohlc.get("close_vs_open_bps") or 0.0)
    volume_rank = volume.get("rank_24h")
    volume_multiple = volume.get("vs_median_24h")
    vwap_distance = vwap.get("vs_session_vwap_bps")
    prior = structure_input.get("prior_hour_range_bps")
    bias = str(structure_input.get("dual_avwap_bias") or "unavailable")
    session = str(structure_input.get("session_label") or "off_session")
    quality = str(quality_input.get("data_quality") or "degraded")
    flags = _derive_flags(inputs)

    if quality != "ok":
        state_label = "degraded_data"
        state_summary = "Feed coverage is degraded; this closed hour is not decision-grade."
    elif flags["wide_range"] and flags["high_volume"]:
        state_label = "expansion"
        state_summary = f"Wide {range_bps:.1f} bps hour with elevated measured participation."
    else:
        state_label = "range"
        state_summary = f"Closed range measured {range_bps:.1f} bps with no expansion tag."

    mattered = [
        f"Range {range_bps:.1f} bps; close versus open {close_vs_open:+.1f} bps",
        (
            f"Volume rank {float(volume_rank) * 100:.0f}% of trailing 24h"
            if volume_rank is not None
            else "Trailing 24h volume rank is unavailable"
        ),
    ]
    if volume_multiple is not None:
        mattered[1] += f"; {float(volume_multiple):.1f}x median"
    if vwap_distance is None:
        mattered.append("Session VWAP distance is unavailable")
    else:
        relation = "above" if float(vwap_distance) > 0 else "below" if float(vwap_distance) < 0 else "at"
        mattered.append(
            f"Close {abs(float(vwap_distance)):.1f} bps {relation} session VWAP"
        )

    prior_text = (
        f"Prior-hour range {float(prior):.1f} bps"
        if prior is not None
        else "Prior-hour range unavailable"
    )
    structure_summary = (
        f"{prior_text}; dual AVWAP tag {bias}; session {session}."
    )
    risks = ["Automated brief — model offline."]
    if quality != "ok":
        gap_minutes = float(quality_input.get("gap_minutes") or 0.0)
        risks.insert(
            0,
            f"Feed/gap quality is {quality}; {gap_minutes:.1f} minutes have unproven coverage.",
        )
    else:
        risks.append("One closed hour does not establish persistence.")
    return {
        "state": {"label": state_label, "summary": state_summary},
        "what_mattered": {"bullets": mattered},
        "structure": {"summary": structure_summary, "bias_tag": bias},
        "risks": {"bullets": risks},
        "watch_next": {
            "summary": (
                "Observe whether the next closed hour's range expands or contracts "
                f"versus {range_bps:.1f} bps."
            )
        },
    }


def _brief_payload_is_valid(payload: Mapping[str, Any]) -> bool:
    required = {
        "schema_version",
        "brief_id",
        "exchange",
        "symbol",
        "hour_open_utc",
        "hour_close_utc",
        "generated_at_utc",
        "model",
        "data_quality",
        "inputs",
        "sections",
        "flags",
        "disclaimer",
    }
    if set(payload) != required:
        return False
    if (
        payload.get("schema_version") != BRIEF_SCHEMA_VERSION
        or payload.get("data_quality") not in _QUALITY_VALUES
        or payload.get("disclaimer") != OBSERVATION_DISCLAIMER
        or not str(payload.get("brief_id") or "")
        or not str(payload.get("model") or "")
    ):
        return False
    inputs = _mapping(payload.get("inputs"))
    sections = _mapping(payload.get("sections"))
    flags = _mapping(payload.get("flags"))
    if not inputs or not _sections_are_safe(sections, inputs):
        return False
    if payload.get("data_quality") != _mapping(inputs.get("quality")).get(
        "data_quality"
    ):
        return False
    if flags != _derive_flags(inputs):
        return False
    try:
        opened = _utc(datetime.fromisoformat(str(payload["hour_open_utc"])), label="open")
        closed = _utc(datetime.fromisoformat(str(payload["hour_close_utc"])), label="close")
        _utc(datetime.fromisoformat(str(payload["generated_at_utc"])), label="generated")
    except ValueError:
        return False
    return closed > opened


class HourAnalysisStore:
    """Small SQLite cache keyed by exchange, symbol, and closed UTC hour."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS hour_analysis (
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                hour_utc TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (exchange, symbol, hour_utc)
            )
            """
        )
        return connection

    def get(self, exchange: str, symbol: str, hour_utc: str) -> HourBrief | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM hour_analysis WHERE exchange=? AND symbol=? AND hour_utc=?",
                (exchange, symbol, hour_utc),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        if not isinstance(payload, Mapping) or not _brief_payload_is_valid(payload):
            return None
        return HourBrief(**payload)

    def put(self, brief: HourBrief) -> None:
        payload = json.dumps(brief.to_dict(), separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO hour_analysis(exchange, symbol, hour_utc, generated_at, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(exchange, symbol, hour_utc) DO UPDATE SET
                    generated_at=excluded.generated_at,
                    payload=excluded.payload
                """,
                (
                    brief.exchange,
                    brief.symbol,
                    brief.hour_open_utc,
                    brief.generated_at_utc,
                    payload,
                ),
            )


class MarketPulseService:
    """Projection service over candle/gap stores; deliberately read-only to runtime."""

    def __init__(
        self,
        candle_root: Path | str,
        gap_root: Path | str,
        analysis_path: Path | str,
        *,
        analyzer: BriefAnalyzer = bounded_observation_brief,
        model: str | None = None,
        clock: Callable[[], datetime] | None = None,
        stale_after: timedelta = timedelta(hours=2),
    ) -> None:
        if stale_after <= timedelta(0):
            raise ValueError("pulse stale_after must be positive")
        self.candle_root = Path(candle_root)
        self.gap_store = GapParquetStore(gap_root)
        self.analysis_store = HourAnalysisStore(analysis_path)
        self.analyzer = analyzer
        self.model = model or (
            "deterministic-fallback-v1"
            if analyzer is bounded_observation_brief
            else "operator-configured"
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.stale_after = stale_after

    def _hours(self, exchange: str, symbol: str) -> list[PulseHour]:
        candles = CandleParquetStore(self.candle_root, exchange=exchange).read(symbol, "1h")
        gaps = self.gap_store.read(exchange, symbol)
        gap_minutes_by_candle = [_gap_minutes(candle, gaps) for candle in candles]
        dual_context = self._dual_avwap_context(
            candles,
            eligible=[minutes == 0 for minutes in gap_minutes_by_candle],
        )
        output: list[PulseHour] = []
        session_date = None
        session_quote = Decimal(0)
        session_volume = Decimal(0)
        volumes: list[float] = []
        ranges: list[float] = []
        for candle, dual, gap_minutes in zip(
            candles,
            dual_context,
            gap_minutes_by_candle,
        ):
            if candle.open_time.date() != session_date:
                session_date = candle.open_time.date()
                session_quote = Decimal(0)
                session_volume = Decimal(0)
            session_quote += candle.quote_volume
            session_volume += candle.volume
            session_vwap = session_quote / session_volume if session_volume > 0 else None
            range_bps = _bps(candle.high - candle.low, candle.open)
            body_bps = _bps(abs(candle.close - candle.open), candle.open)
            close_vs_open = _bps(candle.close - candle.open, candle.open)
            trailing_20 = volumes[-20:]
            volume_median = median(trailing_20) if trailing_20 else None
            trailing_24_median = median(volumes[-24:]) if volumes[-24:] else None
            current_volume = float(candle.volume)
            volume_vs_median = (
                current_volume / volume_median if volume_median is not None and volume_median > 0 else None
            )
            volume_vs_median_24h = (
                current_volume / trailing_24_median
                if trailing_24_median is not None and trailing_24_median > 0
                else None
            )
            trailing_24 = [*volumes[-23:], current_volume]
            volume_rank = (
                sum(value <= current_volume for value in trailing_24) / len(trailing_24)
                if trailing_24
                else None
            )
            vwap_distance = (
                _bps(candle.close - session_vwap, session_vwap)
                if session_vwap is not None
                else None
            )
            output.append(
                PulseHour(
                    symbol=symbol,
                    open_time=_iso(candle.open_time),
                    close_time=_iso(candle.close_time),
                    open=float(candle.open),
                    high=float(candle.high),
                    low=float(candle.low),
                    close=float(candle.close),
                    volume=current_volume,
                    vwap=_number(candle.vwap),
                    session_vwap=_number(session_vwap),
                    range_bps=range_bps,
                    body_bps=body_bps,
                    close_vs_open_bps=close_vs_open,
                    volume_vs_median_20h=volume_vs_median,
                    volume_vs_median_24h=volume_vs_median_24h,
                    volume_rank_24h=volume_rank,
                    vs_session_vwap_bps=vwap_distance,
                    prior_hour_range_bps=ranges[-1] if ranges else None,
                    dual_avwap_bias=dual["bias"],
                    avwap_low=dual["low_value"],
                    avwap_high=dual["high_value"],
                    avwap_low_anchor_utc=dual["low_anchor"],
                    avwap_high_anchor_utc=dual["high_anchor"],
                    avwap_low_confirmed_at_utc=dual["low_confirmed_at"],
                    avwap_high_confirmed_at_utc=dual["high_confirmed_at"],
                    session_active=12 <= candle.open_time.hour < 17,
                    session_label=_session_label(candle.open_time.hour),
                    data_quality="gap" if gap_minutes > 0 else "ok",
                    gap_minutes=gap_minutes,
                    stream_healthy=gap_minutes == 0,
                )
            )
            volumes.append(current_volume)
            ranges.append(range_bps)
        return output

    @staticmethod
    def _dual_avwap_context(
        candles: Sequence[Candle],
        *,
        eligible: Sequence[bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Build causal dual-AVWAP state, failing closed at known gaps."""
        usable = tuple(eligible) if eligible is not None else (True,) * len(candles)
        if len(usable) != len(candles):
            raise ValueError("dual AVWAP eligibility must match candle count")
        anchors = detect_swings(
            candles,
            PULSE_1H_SWING_CONFIG,
            eligible=usable,
        )
        prefix_quote = [Decimal(0)]
        prefix_volume = [Decimal(0)]
        for candle in candles:
            prefix_quote.append(prefix_quote[-1] + candle.quote_volume)
            prefix_volume.append(prefix_volume[-1] + candle.volume)

        def value(anchor: SwingAnchor | None, end: int) -> Decimal | None:
            if anchor is None:
                return None
            quote = prefix_quote[end + 1] - prefix_quote[anchor.index]
            volume = prefix_volume[end + 1] - prefix_volume[anchor.index]
            return quote / volume if volume > 0 else None

        low_anchor: SwingAnchor | None = None
        high_anchor: SwingAnchor | None = None
        anchor_index = 0
        output: list[dict[str, Any]] = []
        for index, candle in enumerate(candles):
            if not usable[index]:
                low_anchor = None
                high_anchor = None
            while (
                anchor_index < len(anchors)
                and anchors[anchor_index].confirmed_at <= candle.close_time
            ):
                anchor = anchors[anchor_index]
                if anchor.kind == SwingKind.LOW:
                    low_anchor = anchor
                else:
                    high_anchor = anchor
                anchor_index += 1
            low_value = value(low_anchor, index)
            high_value = value(high_anchor, index)
            output.append(
                {
                    "bias": dual_avwap_bias(candle.close, low_value, high_value),
                    "low_value": _number(low_value),
                    "high_value": _number(high_value),
                    "low_anchor": _iso(low_anchor.anchor_time) if low_anchor else None,
                    "high_anchor": _iso(high_anchor.anchor_time) if high_anchor else None,
                    "low_confirmed_at": (
                        _iso(low_anchor.confirmed_at) if low_anchor else None
                    ),
                    "high_confirmed_at": (
                        _iso(high_anchor.confirmed_at) if high_anchor else None
                    ),
                }
            )
        return output

    @staticmethod
    def _runtime_degraded(runtime: Mapping[str, Any] | None) -> bool:
        if not runtime:
            return False
        if bool(runtime.get("data_degraded")):
            return True
        feed = runtime.get("feed_health")
        candle_health = feed.get("candles") if isinstance(feed, Mapping) else None
        return bool(candle_health) and str(candle_health).lower() not in {"ok", "live"}

    @staticmethod
    def _forming(runtime: Mapping[str, Any] | None, symbol: str) -> dict[str, Any] | None:
        pulse = runtime.get("pulse") if runtime else None
        forming = pulse.get("forming") if isinstance(pulse, Mapping) else None
        if not isinstance(forming, Mapping) or str(forming.get("symbol") or symbol) != symbol:
            return None
        return dict(forming)

    @staticmethod
    def _symbol_runtime(runtime: Mapping[str, Any] | None, symbol: str) -> bool:
        """Return whether the single runtime quote belongs to this Pulse symbol."""
        if not runtime or not runtime.get("symbol"):
            return True
        runtime_symbol = str(runtime["symbol"]).upper()
        base = symbol.upper().removesuffix("USDT")
        return runtime_symbol.startswith(base)

    @staticmethod
    def _forming_metrics(
        forming: dict[str, Any] | None,
        closed: Sequence[PulseHour],
        *,
        quality: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        if forming is None:
            return None
        enriched = dict(forming)
        try:
            open_price = float(forming["open"])
            high = float(forming["high"])
            low = float(forming["low"])
            close = float(forming["close"])
            volume = float(forming.get("volume") or 0.0)
        except (KeyError, TypeError, ValueError):
            enriched["data_quality"] = quality
            return enriched
        enriched["range_bps"] = (high - low) / open_price * 10_000 if open_price > 0 else 0.0
        enriched["close_vs_open_bps"] = (
            (close - open_price) / open_price * 10_000 if open_price > 0 else 0.0
        )
        enriched["body_bps"] = (
            abs(close - open_price) / open_price * 10_000 if open_price > 0 else 0.0
        )
        trailing = [hour.volume for hour in closed[-24:]]
        volume_median = median(trailing) if trailing else None
        enriched["volume_vs_median_20h"] = (
            volume / volume_median if volume_median is not None and volume_median > 0 else None
        )
        enriched["volume_vs_median_24h"] = enriched["volume_vs_median_20h"]
        enriched["volume_rank_24h"] = (
            sum(value <= volume for value in [*trailing[-23:], volume])
            / len([*trailing[-23:], volume])
            if trailing
            else None
        )
        ranges = [hour.range_bps for hour in closed[-24:]]
        range_median = median(ranges) if ranges else None
        enriched["range_vs_median_24h"] = (
            enriched["range_bps"] / range_median
            if range_median is not None and range_median > 0
            else None
        )
        session_vwap = closed[-1].session_vwap if closed else None
        enriched["vs_session_vwap_bps"] = (
            (close - session_vwap) / session_vwap * 10_000
            if session_vwap is not None and session_vwap > 0
            else None
        )
        enriched["session_label"] = _session_label(now.hour)
        enriched["session_active"] = 12 <= now.hour < 17
        enriched["data_quality"] = quality
        return enriched

    @staticmethod
    def _forming_contract(
        forming: dict[str, Any] | None,
        *,
        now: datetime,
        mid: float | None,
        quality: str,
        latest: PulseHour | None,
    ) -> dict[str, Any]:
        current = dict(forming or {})
        current.setdefault(
            "open_time",
            _iso(now.replace(minute=0, second=0, microsecond=0)),
        )
        raw_open = current["open_time"]
        current["open_time_utc"] = (
            _iso(raw_open) if isinstance(raw_open, datetime) else str(raw_open)
        )
        current["mid"] = mid
        current.setdefault("range_bps", None)
        current.setdefault("body_bps", None)
        current.setdefault("volume", None)
        current.setdefault("volume_rank_24h", None)
        session_vwap = latest.session_vwap if latest is not None else None
        current.setdefault(
            "vs_session_vwap_bps",
            (
                (mid - session_vwap) / session_vwap * 10_000
                if mid is not None and session_vwap is not None and session_vwap > 0
                else None
            ),
        )
        preview_price = mid if mid is not None else _number(current.get("close"))
        preview_bias = dual_avwap_bias(
            preview_price if preview_price is not None else 0,
            latest.avwap_low if latest is not None else None,
            latest.avwap_high if latest is not None else None,
        )
        current["dual_avwap_bias"] = (
            "n/a" if preview_bias == "unavailable" else preview_bias
        )
        current["dual_avwap_reason"] = (
            "no_confirmed_swing_pair"
            if latest is None or latest.avwap_low is None or latest.avwap_high is None
            else "price_unavailable"
            if preview_price is None
            else None
        )
        current["avwap_low"] = latest.avwap_low if latest is not None else None
        current["avwap_high"] = latest.avwap_high if latest is not None else None
        current.setdefault("session_label", _session_label(now.hour))
        current.setdefault("session_active", 12 <= now.hour < 17)
        current["data_quality"] = quality
        current["status"] = (
            "forming"
            if all(current.get(key) is not None for key in ("open", "high", "low", "close"))
            else "awaiting_trades"
        )
        return current

    def hours(self, exchange: str, symbol: str, *, limit: int = 48) -> dict[str, Any]:
        limit = max(1, min(limit, 168))
        rows = self._hours(exchange, symbol)[-limit:]
        return {
            "exchange": exchange,
            "symbol": symbol,
            "hours": [hour.to_dict() for hour in rows],
            "count": len(rows),
            "read_only": True,
        }

    def pulse(
        self,
        exchange: str,
        symbol: str,
        *,
        limit: int = 48,
        runtime: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        rows = self._hours(exchange, symbol)[-max(1, min(limit, 168)) :]
        latest = rows[-1] if rows else None
        gaps = self.gap_store.read(exchange, symbol)
        degraded = self._runtime_degraded(runtime)
        now = _utc(self.clock(), label="pulse clock")
        latest_close = (
            datetime.fromisoformat(latest.close_time)
            if latest is not None
            else None
        )
        stale = latest_close is not None and now - latest_close > self.stale_after
        if (
            latest is not None and latest.data_quality == "gap"
        ) or any(not gap.recovered for gap in gaps):
            pulse_quality = "gap"
        elif degraded or stale:
            pulse_quality = "degraded"
        elif latest is None:
            pulse_quality = "unknown"
        else:
            pulse_quality = "ok"
        alerts = [
            {
                "kind": "gap",
                "at": _iso(gap.detected_at),
                "severity": "warning" if gap.recovered else "critical",
                "message": f"{gap.kind.value}: {gap.detail or 'coverage unproven'}",
                "recovered": gap.recovered,
            }
            for gap in gaps[-12:]
        ]
        if latest is not None:
            alerts.insert(
                0,
                {
                    "kind": "hour_close",
                    "at": latest.close_time,
                    "severity": "info",
                    "message": (
                        f"{symbol} {latest.open_time[11:13]}h closed: "
                        f"{latest.close_vs_open_bps:+.1f} bps, range {latest.range_bps:.1f} bps"
                    ),
                    "recovered": True,
                },
            )
            if (
                latest.volume_rank_24h is not None
                and latest.volume_rank_24h >= 0.9
            ):
                alerts.insert(
                    1,
                    {
                        "kind": "volume_spike",
                        "at": latest.close_time,
                        "severity": "warning",
                        "message": (
                            f"{symbol} closed-hour volume ranked "
                            f"{latest.volume_rank_24h * 100:.0f}th percentile of 24h"
                        ),
                        "recovered": True,
                    },
                )
        if stale and latest_close is not None:
            alerts.insert(
                0,
                {
                    "kind": "stale",
                    "at": _iso(latest_close),
                    "severity": "warning",
                    "message": (
                        f"canonical candle age exceeded "
                        f"{self.stale_after.total_seconds() / 3600:g}h"
                    ),
                    "recovered": False,
                },
            )
        forming_metrics = self._forming_metrics(
            self._forming(runtime, symbol),
            rows,
            quality=pulse_quality,
            now=now,
        )
        runtime_matches = self._symbol_runtime(runtime, symbol)
        feed = runtime.get("feed_health") if runtime and runtime_matches else None
        feed_age_ms = (
            _number(feed.get("last_update_ms"))
            if isinstance(feed, Mapping)
            else None
        )
        latest_gap = gaps[-1] if gaps else None
        book = runtime.get("price") if runtime and runtime_matches else None
        mid = _number(_mapping(book).get("mid"))
        forming = self._forming_contract(
            forming_metrics,
            now=now,
            mid=mid,
            quality=pulse_quality,
            latest=latest,
        )
        fee_wall = DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(
            exchange
        ).taker_round_trip_cost_bps
        return {
            "exchange": exchange,
            "symbol": symbol,
            "as_of": _iso(now),
            "as_of_utc": _iso(now),
            "status": (
                "live"
                if pulse_quality == "ok"
                else "warming" if pulse_quality == "unknown" else "degraded"
            ),
            "data_quality": pulse_quality,
            "forming": forming,
            "hours": [hour.to_dict() for hour in rows],
            "fee_wall_bps": round(fee_wall, 2),
            "session_vwap_series": [
                {
                    "time": int(datetime.fromisoformat(hour.open_time).timestamp()),
                    "value": hour.session_vwap,
                }
                for hour in rows
                if hour.session_vwap is not None
            ],
            "avwap_series": None,
            "dual_avwap_series": {
                "low": [
                    {
                        "time": int(datetime.fromisoformat(hour.open_time).timestamp()),
                        "value": hour.avwap_low,
                    }
                    for hour in rows
                    if hour.avwap_low is not None
                ],
                "high": [
                    {
                        "time": int(datetime.fromisoformat(hour.open_time).timestamp()),
                        "value": hour.avwap_high,
                    }
                    for hour in rows
                    if hour.avwap_high is not None
                ],
            },
            "market": {
                "last": latest.close if latest else None,
                "mid": mid,
                "feed_age_ms": feed_age_ms,
                "canonical_age_ms": (
                    round(max(0.0, (now - latest_close).total_seconds() * 1000.0), 1)
                    if latest_close is not None
                    else None
                ),
                "session_label": _session_label(now.hour),
            },
            "indicators": {
                "session_vwap": latest.session_vwap if latest else None,
                "vs_session_vwap_bps": latest.vs_session_vwap_bps if latest else None,
                "dual_avwap_bias": (
                    "n/a"
                    if latest is None or latest.dual_avwap_bias == "unavailable"
                    else latest.dual_avwap_bias
                ),
                "avwap": None,
                "avwap_label": None,
                "avwap_low": latest.avwap_low if latest else None,
                "avwap_high": latest.avwap_high if latest else None,
                "avwap_low_anchor_utc": latest.avwap_low_anchor_utc if latest else None,
                "avwap_high_anchor_utc": latest.avwap_high_anchor_utc if latest else None,
                "avwap_low_confirmed_at_utc": (
                    latest.avwap_low_confirmed_at_utc if latest else None
                ),
                "avwap_high_confirmed_at_utc": (
                    latest.avwap_high_confirmed_at_utc if latest else None
                ),
                "avwap_unavailable_reason": (
                    "no confirmed swing pair"
                    if latest is None
                    or latest.avwap_low is None
                    or latest.avwap_high is None
                    else None
                ),
            },
            "last_gap": (
                {
                    "kind": latest_gap.kind.value,
                    "start": _iso(latest_gap.start),
                    "end": _iso(latest_gap.end),
                    "recovered": latest_gap.recovered,
                    "detail": latest_gap.detail or "coverage unproven",
                }
                if latest_gap is not None
                else None
            ),
            "book": book,
            "alerts": alerts,
            "policy": OBSERVATION_DISCLAIMER,
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "live_orders_enabled": False,
        }

    def analysis(self, exchange: str, symbol: str, open_time: datetime) -> dict[str, Any]:
        open_time = _utc(open_time, label="analysis hour")
        hour_key = _iso(open_time)
        rows = self._hours(exchange, symbol)
        hour_index = next(
            (index for index, row in enumerate(rows) if row.open_time == hour_key),
            None,
        )
        if hour_index is None:
            raise KeyError(f"no closed 1h candle at {hour_key}")
        hour = rows[hour_index]
        inputs = hour.analysis_inputs(rows[max(0, hour_index - 6) : hour_index])
        cached = self.analysis_store.get(exchange, symbol, hour_key)
        if cached is not None and cached.inputs == inputs:
            return cached.to_dict()
        context = {"inputs": inputs}
        used_model = self.model
        try:
            # Give the adapter a detached JSON copy. Model/plugin code cannot
            # mutate server-owned quality, metrics, flags, or the cached inputs.
            model_context = json.loads(json.dumps(context))
            sections = dict(self.analyzer(model_context))
        except Exception as exc:  # noqa: BLE001 — model boundary must fall back safely
            logger.warning("hour brief analyzer unavailable; using fallback: %s", exc)
            sections = dict(bounded_observation_brief(context))
            used_model = "deterministic-fallback-v1"
        if not _sections_are_safe(sections, inputs):
            sections = dict(bounded_observation_brief(context))
            used_model = "deterministic-fallback-v1"
        flags = _derive_flags(inputs)
        symbol_slug = re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-")
        brief = HourBrief(
            schema_version=BRIEF_SCHEMA_VERSION,
            brief_id=f"{symbol_slug}-{hour.open_time}",
            exchange=exchange,
            symbol=symbol,
            hour_open_utc=hour.open_time,
            hour_close_utc=hour.close_time,
            generated_at_utc=_iso(datetime.now(UTC)),
            model=used_model,
            data_quality=hour.data_quality,
            inputs=inputs,
            sections=sections,
            flags=flags,
        )
        if not _brief_payload_is_valid(brief.to_dict()):
            raise RuntimeError("internal hour brief failed schema validation")
        self.analysis_store.put(brief)
        return brief.to_dict()
