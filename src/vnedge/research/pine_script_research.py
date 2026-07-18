"""Pine-script research knowledge base for public indicator review.

This module is deliberately read-only and artifact-first. TradingView exposes
script metadata and an open-source filter, but many scripts are invite-only or
protected. VNEDGE therefore stores provenance, source hashes, review decisions,
and backtest evidence; it does not vendor bulk third-party Pine source into the
repo.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Literal


PortabilityVerdict = Literal[
    "PORTABLE",
    "PORTABLE_WITH_CHANGES",
    "RESEARCH_ONLY",
    "BLOCKED_NO_SOURCE",
    "BLOCKED_REPAINT_RISK",
]
ScriptKind = Literal["indicator", "strategy", "library", "unknown"]
BacktestStatus = Literal["queued", "running", "passed", "failed", "blocked", "not_applicable"]

DEFAULT_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h")
DEFAULT_VENUES: tuple[str, ...] = ("binanceusdm", "bybit", "delta_india")


@dataclass(frozen=True)
class PineBacktestCell:
    timeframe: str
    status: BacktestStatus = "queued"
    venues: tuple[str, ...] = DEFAULT_VENUES
    samples: int = 0
    avg_net_bps: float | None = None
    profit_factor: float | None = None
    win_rate_pct: float | None = None
    blocker: str = "awaiting VNEDGE port and replay"


@dataclass(frozen=True)
class PineReviewRecord:
    script_id: str
    title: str
    url: str
    author: str = ""
    kind: ScriptKind = "unknown"
    source_available: bool = False
    source_license: str = "unknown"
    source_sha256: str | None = None
    source_lines: int = 0
    tags: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    crypto_portability: PortabilityVerdict = "BLOCKED_NO_SOURCE"
    crypto_fit_score: int = 0
    porting_notes: tuple[str, ...] = ()
    ai_uplift_ideas: tuple[str, ...] = ()
    backtests: tuple[PineBacktestCell, ...] = field(default_factory=tuple)
    decision: str = "WAIT_FOR_PUBLIC_SOURCE"
    reviewed_at: str | None = None
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["backtests"] = [asdict(cell) for cell in self.backtests]
        return payload


def empty_pine_research_payload() -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "fallback_empty",
        "summary": {
            "total": 0,
            "portable": 0,
            "needs_source": 0,
            "research_only": 0,
            "blocked_repaint": 0,
            "backtests_queued": 0,
        },
        "records": [],
        "policy": _policy(),
        "operator_answer": (
            "Pine research KB unavailable; publish research/pine_scripts/"
            "pine_research_kb.json from the reviewer pipeline."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def default_pine_research_payload() -> dict:
    """Seed the page with a serious workflow before the crawler publishes."""

    records = (
        PineReviewRecord(
            script_id="tradingview_catalog",
            title="TradingView public scripts catalog",
            url="https://www.tradingview.com/scripts/",
            author="TradingView community",
            kind="unknown",
            source_available=False,
            tags=("catalog", "discovery", "requires_open_source_filter"),
            features=("metadata_discovery", "open_source_filter"),
            risks=(
                "catalog pages do not reliably expose Pine source in static HTML",
                "protected/invite-only scripts cannot be copied or ported",
            ),
            crypto_portability="BLOCKED_NO_SOURCE",
            crypto_fit_score=15,
            porting_notes=(
                "Use the open-source filter or user-supplied Pine export before porting.",
                "Store source hash/provenance, not bulk source, in the VNEDGE KB.",
            ),
            ai_uplift_ideas=(
                "Cluster scripts by mechanism: trend, breakout, mean reversion, volume, SMC, exits.",
                "Reject duplicate visual overlays unless they add executable edge after fees.",
            ),
            backtests=_queued_backtests("blocked", "source unavailable from catalog page"),
            decision="DISCOVER_OPEN_SOURCE_ONLY",
            reviewed_at=datetime.now(UTC).isoformat(),
        ),
        PineReviewRecord(
            script_id="luxara_live_plan_qtm_v1",
            title="Luxara Live Plan - QTM Matched",
            url="user_supplied_pine:8985eadc",
            author="user supplied",
            kind="indicator",
            source_available=True,
            tags=("atr_trail", "rsi", "structure_midline", "trade_plan"),
            features=("ATR/QTM trail", "EMA/RSI grade", "TP ladder", "expected edge gate"),
            risks=("raw visual labels were negative after cost", "same-data tuned defaults"),
            crypto_portability="PORTABLE_WITH_CHANGES",
            crypto_fit_score=68,
            porting_notes=(
                "Keep QTM flips causal and replace fixed chart points with ATR/bps exits.",
                "Only high-room, high-volume long defaults survived the first VM replay.",
            ),
            ai_uplift_ideas=(
                "Use as a candidate feature inside the edge model, not as a standalone signal.",
                "Retest exact defaults on an untouched window before any paper trial.",
            ),
            backtests=(
                PineBacktestCell(
                    timeframe="15m",
                    status="failed",
                    samples=248,
                    avg_net_bps=27.30,
                    profit_factor=1.61,
                    win_rate_pct=50.8,
                    blocker="same-data research only; needs untouched judgment",
                ),
            )
            + _queued_backtests("queued", "awaiting untouched-window judgment", skip=("15m",)),
            decision="RESEARCH_CANDIDATE_NO_PROMOTION",
            reviewed_at=datetime.now(UTC).isoformat(),
        ),
        PineReviewRecord(
            script_id="luxara_break_bounce_v27_v1",
            title="Luxara Break & Bounce Teaching View V27",
            url="user_supplied_pine:be3cf729",
            author="user supplied",
            kind="indicator",
            source_available=True,
            tags=("range_box", "breakout", "volume_impulse", "tp_sl_plan"),
            features=("prior-bar setup box", "wick preview telemetry", "confirmed close breakout"),
            risks=("broad breakouts failed after costs", "only sparse short/tight-box pulse found"),
            crypto_portability="RESEARCH_ONLY",
            crypto_fit_score=44,
            porting_notes=(
                "Preview labels stay telemetry-only.",
                "Default scanner is short-only A-grade high-volume tight-box, but not promotable.",
            ),
            ai_uplift_ideas=(
                "Keep as feature-engineering input for downside break classifiers.",
                "Require more history before ETH-only untouched judgment.",
            ),
            backtests=(
                PineBacktestCell(
                    timeframe="15m",
                    status="failed",
                    samples=48,
                    avg_net_bps=13.05,
                    profit_factor=None,
                    win_rate_pct=None,
                    blocker="under-sampled and below 25 bps edge floor",
                ),
                PineBacktestCell(
                    timeframe="5m",
                    status="failed",
                    samples=1,
                    avg_net_bps=-63.84,
                    profit_factor=None,
                    win_rate_pct=0.0,
                    blocker="Delta ETH only; no broad 5m data coverage yet",
                ),
            )
            + _queued_backtests("queued", "needs wider timeframe replay", skip=("5m", "15m")),
            decision="TELEMETRY_ONLY",
            reviewed_at=datetime.now(UTC).isoformat(),
        ),
    )
    return build_pine_research_payload(records, source="default_seed")


def load_pine_research_payload(path: Path | None) -> dict:
    """Load a published KB artifact with a defensive fallback."""

    if path is None or not path.exists():
        return default_pine_research_payload()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_pine_research_payload()
    if not isinstance(raw, dict):
        return empty_pine_research_payload()
    records = raw.get("records")
    if not isinstance(records, list):
        return {**empty_pine_research_payload(), "source": str(path)}
    normalized = []
    for record in records:
        if isinstance(record, dict):
            normalized.append(_normalize_record(record))
    return {
        "generated_at": str(raw.get("generated_at") or datetime.now(UTC).isoformat()),
        "source": str(raw.get("source") or path),
        "summary": summarize_records(normalized),
        "records": normalized,
        "policy": raw.get("policy") if isinstance(raw.get("policy"), dict) else _policy(),
        "operator_answer": str(
            raw.get("operator_answer")
            or "Pine research KB loaded; reviews are research-only until backtested."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def build_pine_research_payload(
    records: Iterable[PineReviewRecord],
    *,
    source: str,
) -> dict:
    rows = [record.to_dict() for record in records]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "summary": summarize_records(rows),
        "records": rows,
        "policy": _policy(),
        "operator_answer": (
            "Pine reviews are a research funnel. A script can become a VNEDGE "
            "candidate only after source/provenance, causal port, multi-TF replay, "
            "cost-aware route proof, and untouched-window judgment."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def summarize_records(records: Iterable[dict]) -> dict:
    rows = tuple(records)
    verdicts = Counter(str(row.get("crypto_portability") or "") for row in rows)
    backtests_queued = 0
    for row in rows:
        for cell in row.get("backtests") or []:
            if isinstance(cell, dict) and cell.get("status") == "queued":
                backtests_queued += 1
    return {
        "total": len(rows),
        "portable": verdicts["PORTABLE"] + verdicts["PORTABLE_WITH_CHANGES"],
        "needs_source": verdicts["BLOCKED_NO_SOURCE"],
        "research_only": verdicts["RESEARCH_ONLY"],
        "blocked_repaint": verdicts["BLOCKED_REPAINT_RISK"],
        "backtests_queued": backtests_queued,
    }


def review_pine_source(
    *,
    script_id: str,
    title: str,
    url: str,
    source: str,
    author: str = "",
    source_license: str = "unknown",
) -> PineReviewRecord:
    """Heuristic first-pass review for a user-supplied/open-source Pine file.

    This is not the AI council and not a backtest. It creates a structured
    review seed so the real port/replay work can be queued consistently.
    """

    lower = source.lower()
    kind: ScriptKind = "strategy" if "strategy(" in lower else "indicator" if "indicator(" in lower else "unknown"
    features = _detect_features(lower)
    risks = _detect_risks(lower)
    fit = _crypto_fit_score(features, risks, source)
    verdict: PortabilityVerdict
    if "lookahead_on" in lower or ("request.security" in lower and "barstate.isconfirmed" not in lower):
        verdict = "BLOCKED_REPAINT_RISK"
    elif not source.strip():
        verdict = "BLOCKED_NO_SOURCE"
    elif fit >= 70:
        verdict = "PORTABLE"
    elif fit >= 45:
        verdict = "PORTABLE_WITH_CHANGES"
    else:
        verdict = "RESEARCH_ONLY"
    return PineReviewRecord(
        script_id=script_id,
        title=title,
        url=url,
        author=author,
        kind=kind,
        source_available=bool(source.strip()),
        source_license=source_license,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        source_lines=len(source.splitlines()),
        tags=tuple(sorted(features)),
        features=tuple(sorted(features)),
        risks=tuple(sorted(risks)),
        crypto_portability=verdict,
        crypto_fit_score=fit,
        porting_notes=_porting_notes(features, risks),
        ai_uplift_ideas=_uplift_ideas(features, risks),
        backtests=_queued_backtests(
            "blocked" if verdict.startswith("BLOCKED") else "queued",
            "blocked by source/repaint risk" if verdict.startswith("BLOCKED") else "awaiting causal VNEDGE port",
        ),
        decision="REVIEW_SEED_READY",
        reviewed_at=datetime.now(UTC).isoformat(),
    )


def _queued_backtests(
    status: BacktestStatus,
    blocker: str,
    *,
    skip: tuple[str, ...] = (),
) -> tuple[PineBacktestCell, ...]:
    return tuple(
        PineBacktestCell(timeframe=tf, status=status, blocker=blocker)
        for tf in DEFAULT_TIMEFRAMES
        if tf not in skip
    )


def _normalize_record(record: dict) -> dict:
    out = dict(record)
    out.setdefault("can_trade", False)
    out.setdefault("can_promote", False)
    out.setdefault("requires_untouched_judgment", True)
    out.setdefault("backtests", [])
    out["crypto_fit_score"] = _bounded_int(out.get("crypto_fit_score"), 0, 100)
    return out


def _bounded_int(value: object, floor: int, ceiling: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return floor
    if not math.isfinite(parsed):
        return floor
    return max(floor, min(ceiling, parsed))


def _detect_features(lower_source: str) -> set[str]:
    checks = {
        "trend": ("ema", "supertrend", "adx", "trend"),
        "breakout": ("breakout", "highest", "lowest", "box", "range"),
        "momentum": ("rsi", "roc", "macd", "momentum", "stoch"),
        "volatility": ("atr", "bb", "bollinger", "kc", "volatility"),
        "volume": ("volume", "vwap", "obv", "mfi"),
        "mtf": ("request.security", "timeframe.", "security("),
        "risk_plan": ("stop", "sl", "take", "tp", "strategy.exit"),
        "alerts": ("alertcondition", "alert("),
    }
    return {
        name
        for name, needles in checks.items()
        if any(needle in lower_source for needle in needles)
    }


def _detect_risks(lower_source: str) -> set[str]:
    risks: set[str] = set()
    if "lookahead_on" in lower_source:
        risks.add("lookahead_on")
    if "request.security" in lower_source and "barstate.isconfirmed" not in lower_source:
        risks.add("mtf_repaint_review_required")
    if "plotshape" in lower_source and "strategy." not in lower_source:
        risks.add("visual_label_not_execution_strategy")
    if "barstate.islast" in lower_source:
        risks.add("last_bar_display_logic")
    if "alert(" not in lower_source and "alertcondition" not in lower_source:
        risks.add("no_machine_alert_payload")
    return risks


def _crypto_fit_score(features: set[str], risks: set[str], source: str) -> int:
    score = 35
    for feature in ("trend", "breakout", "momentum", "volatility", "volume", "risk_plan"):
        if feature in features:
            score += 8
    if "alerts" in features:
        score += 6
    if "mtf" in features:
        score += 4
    if "visual_label_not_execution_strategy" in risks:
        score -= 10
    if "mtf_repaint_review_required" in risks:
        score -= 18
    if "lookahead_on" in risks:
        score -= 35
    if len(source.splitlines()) > 900:
        score -= 5
    return max(0, min(100, score))


def _porting_notes(features: set[str], risks: set[str]) -> tuple[str, ...]:
    notes = []
    if "breakout" in features:
        notes.append("Port prior-bar range/level calculations with current bar excluded.")
    if "mtf" in features:
        notes.append("Resample higher timeframes causally and forward-fill only closed HTF bars.")
    if "risk_plan" in features:
        notes.append("Replace visual TP/SL lines with VNEDGE SignalIntent stop/target geometry.")
    if "volume" in features:
        notes.append("Normalize volume by venue/symbol because crypto perp volume scales differ.")
    if "visual_label_not_execution_strategy" in risks:
        notes.append("Treat chart labels as hypotheses; route only cost-clearing intents.")
    if not notes:
        notes.append("Needs manual mechanism extraction before a VNEDGE port.")
    return tuple(notes)


def _uplift_ideas(features: set[str], risks: set[str]) -> tuple[str, ...]:
    ideas = []
    if "trend" in features and "momentum" in features:
        ideas.append("Score trend/momentum agreement as an edge-model feature instead of a hard gate.")
    if "breakout" in features:
        ideas.append("Mine box width, participation, and room-to-liquidity thresholds by venue.")
    if "volatility" in features:
        ideas.append("Condition thresholds on volatility regime to avoid chop overfitting.")
    if "mtf_repaint_review_required" in risks:
        ideas.append("Add a causality audit before any backtest; reject if closed-bar parity fails.")
    if not ideas:
        ideas.append("Cluster against existing VNEDGE scanners and reject duplicates after cost.")
    return tuple(ideas)


def _policy() -> dict:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "source_rule": "review public/open-source/user-supplied Pine only; do not copy protected scripts",
        "promotion_rule": "port -> causality test -> multi-TF replay -> untouched judgment -> shadow/paper",
        "timeframes": list(DEFAULT_TIMEFRAMES),
        "venues": list(DEFAULT_VENUES),
    }
