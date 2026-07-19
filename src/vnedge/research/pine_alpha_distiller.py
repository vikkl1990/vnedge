"""Source-backed Pine alpha distiller.

The Pine Research Lab answers "is source available?"  This module answers the
next question: "what VNEDGE-owned research task should source-backed scripts
be distilled into?"

It is intentionally research-only.  It reads local Pine source artifacts only
to derive hashes, primitive families, and risk flags.  It never emits Pine code
or grants trading permission.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import median
from tempfile import NamedTemporaryFile
from typing import Iterable

from vnedge.research.pine_script_research import (
    DEFAULT_PINE_KB_PATH,
    DEFAULT_PINE_SOURCE_DIR,
    discover_pine_source_files,
    load_pine_research_payload,
)


PINE_ALPHA_DISTILLER_ID = "pine_alpha_distiller_v1"
DEFAULT_OUT = Path("research/live_research/pine_alpha_distiller_latest.json")
DEFAULT_FEED = Path("research/live_research/pine_alpha_distiller_feed.jsonl")
SOURCE_BACKED_PORTABLE = {"PORTABLE", "PORTABLE_WITH_CHANGES"}
REPLAY_TIMEFRAMES = ("5m", "15m", "1h", "4h")
REPLAY_VENUES = ("binanceusdm", "bybit", "delta_india")
PROMOTION_GATES = {
    "expected_net_edge_bps": "> 25",
    "profit_factor": "> 1.5",
    "minimum_historical_trades": ">= 20",
    "taker_route": "only if expected move pays fees, slippage, and safety buffer",
    "judgment": "pre-registered untouched window before paper or shadow promotion",
}


@dataclass(frozen=True)
class SourceArtifact:
    sha256: str
    lines: int
    path_name: str
    text: str


@dataclass(frozen=True)
class ScriptDistillation:
    script_id: str
    title: str
    url: str
    kind: str
    source_sha256: str
    source_lines: int
    source_path_name: str
    crypto_portability: str
    crypto_fit_score: int
    priority_score: int
    mechanism: str
    primitives: tuple[str, ...]
    risks: tuple[str, ...]
    action: str
    recommended_port: str
    distillation_score: int
    source_policy: str = "hash_only_no_pine_code_emitted"
    can_trade: bool = False
    can_promote: bool = False


@dataclass(frozen=True)
class PrimitiveFamily:
    primitive: str
    script_count: int
    recommended_port: str
    required_data: tuple[str, ...]
    gate_before_shadow: tuple[str, ...]
    top_scripts: tuple[dict, ...]


@dataclass(frozen=True)
class PortTask:
    task_id: str
    recommended_port: str
    source_count: int
    source_script_ids: tuple[str, ...]
    primitive_stack: tuple[str, ...]
    median_fit_score: float
    distillation_score: int
    risks: tuple[str, ...]
    required_data: tuple[str, ...]
    first_replay: str
    gate_before_shadow: tuple[str, ...]
    can_trade: bool = False
    can_promote: bool = False


PRIMITIVE_PATTERNS: dict[str, tuple[str, ...]] = {
    "liquidity_zone": (
        r"\bfvg\b",
        r"fair\s+value\s+gap",
        r"order\s+block",
        r"supply",
        r"demand",
        r"liquidity",
        r"imbalance",
        r"premium",
        r"discount",
        r"golden\s+zone",
        r"fib(?:onacci)?",
        r"support",
        r"resistance",
        r"\bs/r\b",
        r"\bsr\b",
    ),
    "sweep_reclaim": (
        r"sweep",
        r"liquidity\s+grab",
        r"stop\s+hunt",
        r"wick",
        r"reclaim",
        r"\bchoch\b",
        r"\bmss\b",
        r"market\s+structure\s+shift",
    ),
    "range_breakout": (
        r"breakout",
        r"break\s+out",
        r"range",
        r"\bbox\b",
        r"consolidation",
        r"compression",
        r"squeeze",
        r"opening\s+range",
        r"session",
    ),
    "trend_trail": (
        r"supertrend",
        r"\btrail",
        r"chandelier",
        r"atr\s+stop",
        r"\but\b",
        r"qtm",
        r"kalman",
        r"parabolic",
        r"\bema\b",
        r"trend",
    ),
    "momentum_confirm": (
        r"\brsi\b",
        r"\bmacd\b",
        r"stoch",
        r"\bbbp\b",
        r"bull\s+bear",
        r"momentum",
        r"\badx\b",
        r"\ber\b",
        r"\broc\b",
        r"divergence",
        r"oscillator",
    ),
    "volume_participation": (
        r"volume",
        r"\bvwap\b",
        r"\bcvd\b",
        r"cumulative\s+volume\s+delta",
        r"footprint",
        r"absorption",
        r"\bmfi\b",
        r"\bobv\b",
        r"profile",
        r"delta",
    ),
    "risk_plan": (
        r"\btp\b",
        r"\bsl\b",
        r"stop\s+loss",
        r"take\s+profit",
        r"target",
        r"\brr\b",
        r"risk\s+reward",
        r"breakeven",
        r"partial",
        r"trailing\s+stop",
    ),
    "mtf_bias": (
        r"request\.security",
        r"\bmtf\b",
        r"multi\s+tf",
        r"multi[-\s]?timeframe",
        r"higher\s+time",
        r"\b1h\b",
        r"\b4h\b",
    ),
}

RISK_PATTERNS: dict[str, tuple[str, ...]] = {
    "lookahead_on": (r"lookahead\s*=\s*barmerge\.lookahead_on", r"lookahead_on"),
    "last_bar_display_state": (r"barstate\.islast", r"barstate\.islastconfirmedhistory"),
    "visual_overlay_state": (r"label\.new", r"plotshape", r"table\.new", r"box\.new", r"line\.new"),
    "fixed_session_review": (r"input\.session", r"\bsession\.", r"\btime\("),
    "forecast_not_signal": (r"forecast", r"prediction", r"future", r"projection"),
}


def run_pine_alpha_distiller(
    *,
    kb_path: Path | str | None = DEFAULT_PINE_KB_PATH,
    source_dir: Path | str | None = DEFAULT_PINE_SOURCE_DIR,
    source_files: Iterable[Path | str] = (),
    max_scripts: int | None = None,
    include_repaint: bool = True,
    now: datetime | None = None,
) -> dict:
    """Build a research-only primitive/task report from source-backed Pine."""

    generated = now or datetime.now(UTC)
    kb = load_pine_research_payload(Path(kb_path) if kb_path is not None else None)
    source_index = build_source_index(source_dir=source_dir, source_files=source_files)
    records = _source_backed_records(kb.get("records", ()), include_repaint=include_repaint)
    if max_scripts is not None:
        records = records[:max_scripts]
    distillations = [
        _distill_script(row, source_index)
        for row in records
        if _source_for_row(row, source_index) is not None
    ]
    primitive_families = _primitive_families(distillations)
    port_tasks = _port_tasks(distillations)
    return {
        "distiller_id": PINE_ALPHA_DISTILLER_ID,
        "generated_at": generated.isoformat(),
        "source": str(kb_path or "default_seed"),
        "source_scope": {
            "source_backed_only": True,
            "source_files_indexed": len(source_index),
            "kb_records": int(kb.get("summary", {}).get("total") or len(kb.get("records", ()))),
            "include_repaint": include_repaint,
            "no_pine_code_in_output": True,
        },
        "summary": _summary(distillations, primitive_families, port_tasks),
        "policy": _policy(),
        "promotion_gates": PROMOTION_GATES,
        "primitive_families": [asdict(row) for row in primitive_families],
        "port_tasks": [asdict(row) for row in port_tasks],
        "script_distillations": [asdict(row) for row in distillations],
        "operator_answer": _operator_answer(distillations, port_tasks),
        "can_trade": False,
        "can_promote": False,
    }


def publish_pine_alpha_distiller(
    payload: dict,
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    feed_path = Path(feed)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _assert_no_pine_source_leak(encoded)
    with NamedTemporaryFile(
        "w",
        dir=out_path.parent,
        prefix=out_path.name,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.chmod(0o644)
    tmp_path.replace(out_path)
    out_path.chmod(0o644)
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    with feed_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    feed_path.chmod(0o644)
    return out_path


def build_source_index(
    *,
    source_dir: Path | str | None = DEFAULT_PINE_SOURCE_DIR,
    source_files: Iterable[Path | str] = (),
) -> dict[str, SourceArtifact]:
    out: dict[str, SourceArtifact] = {}
    for path in discover_pine_source_files(source_dir, source_files):
        text = path.read_text(encoding="utf-8", errors="replace")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        out.setdefault(
            digest,
            SourceArtifact(
                sha256=digest,
                lines=len(text.splitlines()),
                path_name=path.name,
                text=text,
            ),
        )
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="distill source-backed Pine corpus")
    parser.add_argument("--kb", default=str(DEFAULT_PINE_KB_PATH))
    parser.add_argument("--source-dir", default=str(DEFAULT_PINE_SOURCE_DIR))
    parser.add_argument("--source-file", action="append", default=[])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-scripts", type=int)
    parser.add_argument(
        "--include-repaint",
        action="store_true",
        help="deprecated; repaint-risk rows are included in quarantine by default",
    )
    parser.add_argument(
        "--portable-only",
        action="store_true",
        help="exclude repaint-risk rows and emit only portable port candidates",
    )
    parser.add_argument("--no-write", action="store_true", help="print JSON only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run_pine_alpha_distiller(
        kb_path=args.kb,
        source_dir=args.source_dir,
        source_files=args.source_file,
        max_scripts=args.max_scripts,
        include_repaint=args.include_repaint or not args.portable_only,
    )
    if args.no_write:
        encoded = json.dumps(payload, indent=2, sort_keys=True)
        _assert_no_pine_source_leak(encoded)
        print(encoded)
        return 0
    path = publish_pine_alpha_distiller(payload, out=args.out, feed=args.feed)
    print(path)
    return 0


def _source_backed_records(records: Iterable[dict], *, include_repaint: bool) -> list[dict]:
    out = [
        dict(row)
        for row in records
        if row.get("source_available")
        and (
            include_repaint
            or str(row.get("crypto_portability") or "") in SOURCE_BACKED_PORTABLE
        )
    ]
    return sorted(
        out,
        key=lambda row: (
            -int(row.get("priority_score") or 0),
            str(row.get("script_id") or ""),
        ),
    )


def _distill_script(row: dict, source_index: dict[str, SourceArtifact]) -> ScriptDistillation:
    artifact = _source_for_row(row, source_index)
    if artifact is None:
        raise ValueError(f"source artifact missing for {row.get('script_id')}")
    text = _analysis_text(row, artifact.text)
    primitives = _detect_primitives(text)
    risks = _detect_risks(text, row)
    recommended_port = _recommended_port(row, primitives, risks)
    action = _action_for(row, recommended_port, risks)
    return ScriptDistillation(
        script_id=str(row.get("script_id") or ""),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        kind=str(row.get("kind") or "unknown"),
        source_sha256=artifact.sha256,
        source_lines=artifact.lines,
        source_path_name=artifact.path_name,
        crypto_portability=str(row.get("crypto_portability") or ""),
        crypto_fit_score=_bounded_int(row.get("crypto_fit_score"), 0, 100),
        priority_score=_bounded_int(row.get("priority_score"), 0, 100),
        mechanism=str(row.get("mechanism") or "general"),
        primitives=primitives,
        risks=risks,
        action=action,
        recommended_port=recommended_port,
        distillation_score=_distillation_score(row, primitives, risks, recommended_port),
    )


def _source_for_row(row: dict, source_index: dict[str, SourceArtifact]) -> SourceArtifact | None:
    digest = str(row.get("source_sha256") or "").strip()
    if not digest:
        extraction = row.get("source_extraction")
        if isinstance(extraction, dict):
            digest = str(extraction.get("source_sha256") or "").strip()
    return source_index.get(digest) if digest else None


def _analysis_text(row: dict, source: str) -> str:
    metadata = " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("script_id") or ""),
            str(row.get("mechanism") or ""),
            " ".join(str(item) for item in row.get("features") or ()),
            " ".join(str(item) for item in row.get("tags") or ()),
            " ".join(str(item) for item in row.get("porting_notes") or ()),
        ]
    )
    return f"{metadata}\n{source}".lower()


def _detect_primitives(text: str) -> tuple[str, ...]:
    found = [
        primitive
        for primitive, patterns in PRIMITIVE_PATTERNS.items()
        if _matches_any(text, patterns)
    ]
    if not found:
        found.append("feature_bank")
    return tuple(found)


def _detect_risks(text: str, row: dict) -> tuple[str, ...]:
    risks = {str(item) for item in row.get("risks") or () if str(item).strip()}
    for risk, patterns in RISK_PATTERNS.items():
        if _matches_any(text, patterns):
            risks.add(risk)
    if "request.security" in text and "barstate.isconfirmed" not in text:
        risks.add("request_security_requires_closed_bar_rewrite")
    if "strategy(" not in text and "alertcondition(" not in text:
        risks.add("no_machine_alert_contract")
    if "library(" in text or str(row.get("kind") or "") == "library":
        risks.add("library_helper_not_standalone")
    if "strategy.position_size" in text or "pyramiding" in text:
        risks.add("strategy_state_requires_event_rewrite")
    return tuple(sorted(risks))


def _recommended_port(row: dict, primitives: tuple[str, ...], risks: tuple[str, ...]) -> str:
    primitive_set = set(primitives)
    risk_set = set(risks)
    row_text = " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("script_id") or ""),
            str(row.get("mechanism") or ""),
            " ".join(str(item) for item in row.get("features") or ()),
            " ".join(str(item) for item in row.get("tags") or ()),
        ]
    ).lower()
    if (
        str(row.get("crypto_portability") or "") == "BLOCKED_REPAINT_RISK"
        or "lookahead_on" in risk_set
        or "request_security_requires_closed_bar_rewrite" in risk_set
    ):
        return "causality_quarantine_v1"
    if "library_helper_not_standalone" in risk_set:
        return "source_feature_library_review_v1"
    if "volume_participation" in primitive_set and (
        str(row.get("mechanism") or "") == "orderflow"
        or any(token in row_text for token in ("cvd", "footprint", "absorption", "volume delta"))
    ):
        return "orderflow_proxy_v1"
    if "liquidity_zone" in primitive_set and _has_any_primitive(
        primitive_set,
        ("sweep_reclaim", "range_breakout"),
    ):
        return "fvg_liquidity_breakout_v1"
    if "range_breakout" in primitive_set and "volume_participation" in primitive_set:
        return "range_expansion_breakout_v1"
    if "trend_trail" in primitive_set and "risk_plan" in primitive_set:
        return "trail_exit_lab_v1"
    if "momentum_confirm" in primitive_set and "trend_trail" in primitive_set:
        return "trend_momentum_context_v1"
    return "edge_model_feature_bank_v1"


def _action_for(row: dict, recommended_port: str, risks: tuple[str, ...]) -> str:
    risk_set = set(risks)
    verdict = str(row.get("crypto_portability") or "")
    if recommended_port == "causality_quarantine_v1":
        return "CAUSALITY_QUARANTINE"
    if "library_helper_not_standalone" in risk_set or verdict == "RESEARCH_ONLY":
        return "FEATURE_BANK_ONLY"
    if verdict in SOURCE_BACKED_PORTABLE:
        return "PORT_CANDIDATE"
    return "MANUAL_REVIEW"


def _distillation_score(
    row: dict,
    primitives: tuple[str, ...],
    risks: tuple[str, ...],
    recommended_port: str,
) -> int:
    score = int(round(
        _bounded_int(row.get("crypto_fit_score"), 0, 100) * 0.62
        + _bounded_int(row.get("priority_score"), 0, 100) * 0.18
    ))
    score += min(16, len(primitives) * 2)
    score += {
        "orderflow_proxy_v1": 10,
        "fvg_liquidity_breakout_v1": 9,
        "range_expansion_breakout_v1": 8,
        "trail_exit_lab_v1": 7,
        "trend_momentum_context_v1": 5,
        "edge_model_feature_bank_v1": 2,
        "source_feature_library_review_v1": -12,
        "causality_quarantine_v1": -40,
    }.get(recommended_port, 0)
    risk_set = set(risks)
    if "no_machine_alert_contract" in risk_set:
        score -= 6
    if "visual_overlay_state" in risk_set:
        score -= 8
    if "forecast_not_signal" in risk_set:
        score -= 8
    if "strategy_state_requires_event_rewrite" in risk_set:
        score -= 6
    return max(0, min(100, score))


def _primitive_families(distillations: Iterable[ScriptDistillation]) -> list[PrimitiveFamily]:
    by_primitive: dict[str, list[ScriptDistillation]] = defaultdict(list)
    for row in distillations:
        if row.action != "PORT_CANDIDATE":
            continue
        for primitive in row.primitives:
            by_primitive[primitive].append(row)
    families = []
    for primitive, rows in by_primitive.items():
        sorted_rows = sorted(rows, key=lambda row: (-row.distillation_score, row.script_id))
        port = _dominant_port(sorted_rows)
        families.append(
            PrimitiveFamily(
                primitive=primitive,
                script_count=len(sorted_rows),
                recommended_port=port,
                required_data=_required_data(port),
                gate_before_shadow=_gate_before_shadow(port),
                top_scripts=tuple(_top_script(row) for row in sorted_rows[:8]),
            )
        )
    return sorted(families, key=lambda row: (-row.script_count, row.primitive))


def _port_tasks(distillations: Iterable[ScriptDistillation]) -> list[PortTask]:
    by_port: dict[str, list[ScriptDistillation]] = defaultdict(list)
    for row in distillations:
        if row.action != "PORT_CANDIDATE":
            continue
        by_port[row.recommended_port].append(row)
    tasks = []
    for port, rows in by_port.items():
        sorted_rows = sorted(rows, key=lambda row: (-row.distillation_score, row.script_id))
        primitive_counts = Counter(
            primitive
            for row in sorted_rows
            for primitive in row.primitives
            if primitive != "feature_bank"
        )
        risk_counts = Counter(risk for row in sorted_rows for risk in row.risks)
        fit_scores = [row.crypto_fit_score for row in sorted_rows]
        score = _task_score(sorted_rows)
        tasks.append(
            PortTask(
                task_id=f"{PINE_ALPHA_DISTILLER_ID}|{port}",
                recommended_port=port,
                source_count=len(sorted_rows),
                source_script_ids=tuple(row.script_id for row in sorted_rows[:12]),
                primitive_stack=tuple(
                    primitive for primitive, _ in primitive_counts.most_common(6)
                ),
                median_fit_score=round(float(median(fit_scores)), 2) if fit_scores else 0.0,
                distillation_score=score,
                risks=tuple(risk for risk, _ in risk_counts.most_common(6)),
                required_data=_required_data(port),
                first_replay=_first_replay(port),
                gate_before_shadow=_gate_before_shadow(port),
            )
        )
    return sorted(tasks, key=lambda row: (-row.distillation_score, -row.source_count, row.recommended_port))


def _summary(
    distillations: list[ScriptDistillation],
    primitive_families: list[PrimitiveFamily],
    port_tasks: list[PortTask],
) -> dict:
    actions = Counter(row.action for row in distillations)
    primitives = Counter(primitive for row in distillations for primitive in row.primitives)
    ports = Counter(row.recommended_port for row in distillations)
    port_ready = actions.get("PORT_CANDIDATE", 0)
    return {
        "source_backed_reviewed": len(distillations),
        "port_candidates": port_ready,
        "causality_quarantine": actions.get("CAUSALITY_QUARANTINE", 0),
        "feature_bank_only": actions.get("FEATURE_BANK_ONLY", 0),
        "manual_review": actions.get("MANUAL_REVIEW", 0),
        "primitive_families": len(primitive_families),
        "port_tasks": len(port_tasks),
        "primitive_counts": dict(primitives.most_common()),
        "task_counts": dict(ports.most_common()),
        "top_task": port_tasks[0].recommended_port if port_tasks else "",
        "top_task_source_count": port_tasks[0].source_count if port_tasks else 0,
        "queued_backtest_cells": port_ready * len(REPLAY_TIMEFRAMES) * len(REPLAY_VENUES),
        "can_trade": False,
        "can_promote": False,
    }


def _policy() -> dict:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "source_rule": "source-backed rows only; output keeps hashes and metadata, never Pine code",
        "copying_policy": "distill primitives, then implement VNEDGE-owned causal Python",
        "requires_causal_port": True,
        "requires_backtest": True,
        "requires_untouched_judgment": True,
        "requires_human_approval": True,
    }


def _operator_answer(distillations: list[ScriptDistillation], port_tasks: list[PortTask]) -> str:
    if not distillations:
        return (
            "No source-backed portable Pine rows were found. Retry open-source extraction "
            "or supply Pine exports before VNEDGE can distill anything honestly."
        )
    if not port_tasks:
        return (
            "The source-backed rows are mostly quarantine/feature-bank material. Do not "
            "promote; first remove repaint/display-state risks."
        )
    top = port_tasks[0]
    return (
        f"Highest-ranked source-backed build is {top.recommended_port} from "
        f"{top.source_count} scripts. Next step is a VNEDGE-owned causal port, "
        "then replay on 5m/15m/1h/4h across Binance, Bybit, and Delta India."
    )


def _dominant_port(rows: list[ScriptDistillation]) -> str:
    if not rows:
        return "edge_model_feature_bank_v1"
    return Counter(row.recommended_port for row in rows).most_common(1)[0][0]


def _top_script(row: ScriptDistillation) -> dict:
    return {
        "script_id": row.script_id,
        "title": row.title,
        "url": row.url,
        "source_sha256": row.source_sha256[:12],
        "fit": row.crypto_fit_score,
        "distillation_score": row.distillation_score,
    }


def _task_score(rows: list[ScriptDistillation]) -> int:
    if not rows:
        return 0
    base = (sum(row.distillation_score for row in rows[:20]) / min(len(rows), 20)) * 0.82
    count_bonus = min(18, int(math.log2(len(rows) + 1) * 4))
    diversity_bonus = min(10, len({primitive for row in rows for primitive in row.primitives}))
    return max(0, min(100, int(round(base + count_bonus + diversity_bonus))))


def _required_data(port: str) -> tuple[str, ...]:
    common = ("fee_model", "slippage_model", "closed_candles")
    mapping = {
        "fvg_liquidity_breakout_v1": (
            "1h_bias_candles",
            "15m_zone_builder",
            "5m_displacement_trigger",
            "volume_zscore",
            "room_to_liquidity",
        ),
        "range_expansion_breakout_v1": (
            "15m_range_box",
            "5m_break_retest",
            "volume_impulse",
            "atr_percentile",
            "session_24x7_crypto_guard",
        ),
        "trail_exit_lab_v1": (
            "entry_journal",
            "5m_to_15m_ohlcv",
            "atr_trail",
            "tp1_be_state",
            "stop_first_backtest",
        ),
        "orderflow_proxy_v1": (
            "public_trades",
            "cvd_delta_bars",
            "l2_snapshot_coverage",
            "conservative_fill_model",
            "spread_depth_filter",
        ),
        "trend_momentum_context_v1": (
            "1h_trend_bias",
            "15m_momentum_state",
            "5m_trigger",
            "adx_er_alignment",
            "volume_confirmation",
        ),
        "edge_model_feature_bank_v1": (
            "scanner_opportunity_rows",
            "chronological_train_oos_split",
            "train_only_calibration",
            "model_drift_telemetry",
        ),
    }
    return tuple((*mapping.get(port, ("feature_matrix", "causality_audit")), *common))


def _first_replay(port: str) -> str:
    mapping = {
        "fvg_liquidity_breakout_v1": "ETH/SOL/XRP 5m trigger with 15m zones and 1h bias; require expected net >25 bps.",
        "range_expansion_breakout_v1": "All liquid pairs, 15m range setup and 5m close-confirmed break/retest.",
        "trail_exit_lab_v1": "Apply as an exit overlay to existing shadow/paper entries before standalone entry tests.",
        "orderflow_proxy_v1": "Use recorded trades/L2 only; replay with maker-first and taker-fallback fee wall.",
        "trend_momentum_context_v1": "Use as context permission for active scanner lanes before standalone entries.",
        "edge_model_feature_bank_v1": "Train edge_router features; compare OOS selected subset against raw scanner baseline.",
    }
    return mapping.get(port, "Run causal replay before any promotion.")


def _gate_before_shadow(port: str) -> tuple[str, ...]:
    gates = [
        "source hash recorded and no Pine code copied into runtime",
        "causality audit passes: no lookahead, no unconfirmed HTF bars",
        "multi-timeframe replay covers 5m, 15m, 1h, and 4h where applicable",
        "expected net edge >25 bps after fees, slippage, and safety buffer",
        "PF >1.5 and at least 20 historical trades",
        "untouched-window judgment passes before paper or shadow promotion",
    ]
    if port == "trail_exit_lab_v1":
        gates.append("improves existing entry exits OOS without increasing drawdown")
    if port == "orderflow_proxy_v1":
        gates.append("public-trade and L2 coverage pass recorder freshness checks")
    return tuple(gates)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _has_any_primitive(primitives: set[str], needles: tuple[str, ...]) -> bool:
    return any(needle in primitives for needle in needles)


def _bounded_int(value: object, floor: int, ceiling: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return floor
    if not math.isfinite(parsed):
        return floor
    return max(floor, min(ceiling, parsed))


def _assert_no_pine_source_leak(encoded: str) -> None:
    forbidden = ("//@version", "indicator(", "strategy(", "library(", "plotshape", "label.new")
    if any(token in encoded for token in forbidden):
        raise ValueError("distiller output contains Pine source-like text")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
