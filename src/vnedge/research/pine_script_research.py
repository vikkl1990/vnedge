"""Pine-script research knowledge base for public indicator review.

This module is deliberately read-only and artifact-first. TradingView exposes
script metadata and an open-source filter, but many scripts are invite-only or
protected. VNEDGE therefore stores provenance, source hashes, review decisions,
and backtest evidence; it does not vendor bulk third-party Pine source into the
repo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from html import unescape
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Literal
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


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
DEFAULT_PINE_SOURCE_DIR = Path("research/pine_scripts/sources")
DEFAULT_PINE_KB_PATH = Path("research/pine_scripts/pine_research_kb.json")
PINE_SOURCE_SUFFIXES = (".pine", ".pinescript", ".txt")
TRADINGVIEW_BASE_URL = "https://www.tradingview.com"
TRADINGVIEW_SCRIPT_RE = re.compile(
    r"(?:https?://(?:www\.|in\.)?tradingview\.com)?/script/"
    r"[A-Za-z0-9]+-[^\"'<>\s?#]+/?(?:#[^\"'<>\s]+)?"
)


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
            "source_backed": 0,
            "catalog_only": 0,
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


def publish_pine_research_kb(
    *,
    source_dir: Path | str | None = DEFAULT_PINE_SOURCE_DIR,
    source_files: Iterable[Path | str] = (),
    catalog_urls: Iterable[str] = (),
    catalog_html_files: Iterable[Path | str] = (),
    output_path: Path | str = DEFAULT_PINE_KB_PATH,
    include_defaults: bool = True,
    max_catalog_records: int = 250,
    source_label: str = "pine_research_publisher",
) -> dict:
    """Review public/user-supplied Pine files and write the dashboard KB.

    This intentionally works from local files only. TradingView protected or
    invite-only source must not be scraped into VNEDGE; those records stay
    metadata-only until the user supplies lawful source.
    """

    records: list[dict] = []
    if include_defaults:
        records.extend(default_pine_research_payload()["records"])
    for path in discover_pine_source_files(source_dir, source_files):
        source = path.read_text(encoding="utf-8")
        record = review_pine_source(
            script_id=_script_id_from_path(path),
            title=_title_from_source(source, path.stem),
            url=f"user_supplied_pine:{path.name}",
            source=source,
            author=_author_from_source(source),
            source_license=_license_from_source(source),
        )
        records.append(record.to_dict())
    for url in discover_tradingview_catalog_urls(
        catalog_urls=catalog_urls,
        catalog_html_files=catalog_html_files,
        max_records=max_catalog_records,
    ):
        records.append(review_tradingview_catalog_script(url).to_dict())
    payload = _build_payload_from_dicts(
        _dedupe_record_dicts(records),
        source=source_label,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def discover_pine_source_files(
    source_dir: Path | str | None = DEFAULT_PINE_SOURCE_DIR,
    source_files: Iterable[Path | str] = (),
) -> tuple[Path, ...]:
    """Return local Pine source files in stable order."""

    discovered: list[Path] = []
    for item in source_files:
        path = Path(item)
        if path.exists() and path.is_file() and _is_pine_source_path(path):
            discovered.append(path)
    if source_dir is not None:
        root = Path(source_dir)
        if root.exists():
            discovered.extend(
                path for path in root.rglob("*")
                if path.is_file() and _is_pine_source_path(path)
            )
    seen: set[Path] = set()
    out: list[Path] = []
    for path in sorted(discovered, key=lambda p: str(p)):
        resolved = path.resolve()
        if resolved in seen:
            continue
        out.append(path)
        seen.add(resolved)
    return tuple(out)


def discover_tradingview_catalog_urls(
    *,
    catalog_urls: Iterable[str] = (),
    catalog_html_files: Iterable[Path | str] = (),
    max_records: int = 250,
) -> tuple[str, ...]:
    """Discover TradingView script URLs from catalog/profile/tag pages.

    TradingView's public catalog exposes script cards in HTML for some pages
    and via client-side rendering for others. This importer intentionally
    collects URL metadata only; Pine source must arrive via open/user-supplied
    local files before any strategy port or replay can happen.
    """

    urls: list[str] = []
    for url in catalog_urls:
        cleaned = _clean_url(url)
        if not cleaned:
            continue
        if _is_tradingview_script_url(cleaned):
            urls.append(cleaned)
            continue
        html = _fetch_catalog_html(cleaned)
        urls.extend(extract_tradingview_script_urls(html, base_url=cleaned))
    for item in catalog_html_files:
        path = Path(item)
        if not path.exists() or not path.is_file():
            continue
        urls.extend(
            extract_tradingview_script_urls(
                path.read_text(encoding="utf-8"),
                base_url=TRADINGVIEW_BASE_URL,
            )
        )
    return _dedupe_urls(urls, limit=max_records)


def extract_tradingview_script_urls(
    html: str,
    *,
    base_url: str = TRADINGVIEW_BASE_URL,
) -> tuple[str, ...]:
    """Extract normalized TradingView script URLs from a public HTML payload."""

    found = []
    for match in TRADINGVIEW_SCRIPT_RE.finditer(html):
        found.append(_normalize_tradingview_url(match.group(0), base_url=base_url))
    return _dedupe_urls(found)


def review_tradingview_catalog_script(url: str) -> PineReviewRecord:
    """Create a metadata-only research row for a TradingView script URL."""

    normalized = _normalize_tradingview_url(url)
    slug = _slug_from_script_url(normalized)
    title = _title_from_slug(slug)
    lower = title.lower()
    features = _detect_features(lower)
    risks = {
        "catalog_metadata_only",
        "source_not_collected",
    }
    if not features:
        features = _detect_features(slug.lower().replace("-", " "))
    fit = max(5, min(55, _crypto_fit_score(features, risks, lower) - 18))
    return PineReviewRecord(
        script_id=_script_id_from_url(normalized),
        title=title,
        url=normalized,
        author="TradingView community",
        kind="unknown",
        source_available=False,
        source_license="unknown",
        source_lines=0,
        tags=tuple(sorted({"catalog", *features})),
        features=tuple(sorted(features)),
        risks=tuple(sorted(risks)),
        crypto_portability="BLOCKED_NO_SOURCE",
        crypto_fit_score=fit,
        porting_notes=(
            "Catalog discovery only: fetch or supply lawful open Pine source before porting.",
            "Do not treat screenshots, labels, likes, or comments as executable edge.",
        ),
        ai_uplift_ideas=(
            "Prioritize source requests for scripts with crypto-fit mechanics matching VNEDGE gaps.",
            "Cluster catalog backlog by mechanism, then port one representative per cluster.",
        ),
        backtests=_queued_backtests("blocked", "catalog URL discovered; Pine source not available"),
        decision="WAIT_FOR_OPEN_SOURCE_OR_USER_EXPORT",
        reviewed_at=datetime.now(UTC).isoformat(),
    )


def _build_payload_from_dicts(records: Iterable[dict], *, source: str) -> dict:
    rows = [_normalize_record(record) for record in records]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "summary": summarize_records(rows),
        "records": rows,
        "policy": _policy(),
        "operator_answer": (
            "Pine KB published from local public/user-supplied source files. "
            "Every record remains research-only until VNEDGE port/replay and "
            "untouched-window judgment clear."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def _fetch_catalog_html(url: str, *, timeout_seconds: float = 20.0) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 VNEDGE-PineResearch/1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return response.read().decode("utf-8", errors="replace")
    except (OSError, URLError, TimeoutError, ValueError):
        return ""


def _dedupe_record_dicts(records: Iterable[dict]) -> tuple[dict, ...]:
    by_id: dict[str, dict] = {}
    for record in records:
        script_id = str(record.get("script_id") or "").strip()
        if not script_id:
            continue
        existing = by_id.get(script_id)
        if existing is None or _record_priority(record) >= _record_priority(existing):
            by_id[script_id] = dict(record)
    return tuple(by_id[key] for key in sorted(by_id))


def _record_priority(record: dict) -> tuple[int, int]:
    return (
        1 if record.get("source_available") else 0,
        int(record.get("source_lines") or 0),
    )


def summarize_records(records: Iterable[dict]) -> dict:
    rows = tuple(records)
    verdicts = Counter(str(row.get("crypto_portability") or "") for row in rows)
    source_backed = sum(1 for row in rows if row.get("source_available"))
    catalog_only = sum(
        1
        for row in rows
        if not row.get("source_available") and "/script/" in str(row.get("url") or "")
    )
    backtests_queued = 0
    for row in rows:
        for cell in row.get("backtests") or []:
            if isinstance(cell, dict) and cell.get("status") == "queued":
                backtests_queued += 1
    return {
        "total": len(rows),
        "source_backed": source_backed,
        "catalog_only": catalog_only,
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


def _is_pine_source_path(path: Path) -> bool:
    return path.suffix.lower() in PINE_SOURCE_SUFFIXES


def _clean_url(url: str) -> str:
    return str(url or "").strip()


def _dedupe_urls(urls: Iterable[str], *, limit: int | None = None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        normalized = _normalize_tradingview_url(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
        if limit is not None and len(out) >= limit:
            break
    return tuple(out)


def _normalize_tradingview_url(
    url: str,
    *,
    base_url: str = TRADINGVIEW_BASE_URL,
) -> str:
    joined = urljoin(base_url, unescape(str(url or "").strip()))
    parsed = urlparse(joined)
    if "/script/" not in parsed.path:
        return ""
    path = parsed.path
    if not path.endswith("/"):
        path = f"{path}/"
    return f"https://www.tradingview.com{path}"


def _is_tradingview_script_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.endswith("tradingview.com") and parsed.path.startswith("/script/")


def _slug_from_script_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "script":
        return parts[1]
    return "tradingview-script"


def _script_id_from_url(url: str) -> str:
    return _script_id_from_path(Path(_slug_from_script_url(url)))


def _title_from_slug(slug: str) -> str:
    pieces = slug.split("-", 1)
    title_slug = pieces[1] if len(pieces) == 2 else pieces[0]
    title = re.sub(r"[-_]+", " ", title_slug).strip()
    return title.title() if title else "TradingView Script"


def _script_id_from_path(path: Path) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", path.stem.strip()).strip("_").lower()
    return cleaned or "pine_script"


def _title_from_source(source: str, fallback: str) -> str:
    match = re.search(
        r"\b(?:indicator|strategy|library)\s*\(\s*['\"]([^'\"]+)['\"]",
        source,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else fallback.replace("_", " ").strip()


def _author_from_source(source: str) -> str:
    for line in source.splitlines()[:20]:
        match = re.search(r"(?:author|created by)\s*[:=]\s*(.+)$", line, re.IGNORECASE)
        if match:
            return match.group(1).strip().strip("/ ")
    return "user supplied"


def _license_from_source(source: str) -> str:
    lower = source.lower()
    if "mozilla public license" in lower or "mpl-2.0" in lower:
        return "MPL-2.0"
    if "mit license" in lower:
        return "MIT"
    if "apache license" in lower:
        return "Apache-2.0"
    return "unknown"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="publish a research-only Pine script review KB artifact"
    )
    parser.add_argument("source_files", nargs="*", help="explicit Pine source files")
    parser.add_argument(
        "--source-dir",
        default=str(DEFAULT_PINE_SOURCE_DIR),
        help="directory scanned recursively for .pine/.pinescript/.txt files",
    )
    parser.add_argument("--output", default=str(DEFAULT_PINE_KB_PATH))
    parser.add_argument(
        "--catalog-url",
        action="append",
        default=[],
        help="TradingView catalog/profile/tag/script URL to discover as metadata-only backlog",
    )
    parser.add_argument(
        "--catalog-html",
        action="append",
        default=[],
        help="saved TradingView HTML file to parse for script URLs",
    )
    parser.add_argument(
        "--max-catalog-records",
        type=int,
        default=250,
        help="maximum discovered TradingView script URLs to add",
    )
    parser.add_argument(
        "--no-defaults",
        action="store_true",
        help="do not include built-in seed records",
    )
    parser.add_argument("--json", action="store_true", help="print full payload")
    args = parser.parse_args(argv)

    payload = publish_pine_research_kb(
        source_dir=args.source_dir,
        source_files=args.source_files,
        catalog_urls=args.catalog_url,
        catalog_html_files=args.catalog_html,
        output_path=args.output,
        include_defaults=not args.no_defaults,
        max_catalog_records=max(0, args.max_catalog_records),
        source_label="pine_research_publisher",
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        summary = payload["summary"]
        print(
            "pine research KB published: "
            f"total={summary['total']} portable={summary['portable']} "
            f"needs_source={summary['needs_source']} "
            f"queued={summary['backtests_queued']} output={args.output}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
