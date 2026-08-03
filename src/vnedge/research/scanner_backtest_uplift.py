"""Backtest-failure uplift planner for scanner evidence.

This module consumes completed scanner/replay evidence and turns every row into
an operator-useful diagnosis: what failed, how far it is from the fee wall, and
what the next research experiment should be. It is intentionally research-only;
it never grants paper/live permission by itself.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Iterable, Literal

try:  # Keep this report useful in tiny test harnesses that do not import strategies.
    from vnedge.strategy.strategy_registry import STRATEGIES
except Exception:  # pragma: no cover - defensive fallback for partial environments
    STRATEGIES = {}


SCANNER_BACKTEST_UPLIFT_ID = "scanner_backtest_uplift_v1"
DEFAULT_OUT = Path("research/live_research/scanner_backtest_uplift_latest.json")
DEFAULT_FEED = Path("research/live_research/scanner_backtest_uplift_feed.jsonl")

FailureMode = Literal[
    "PROMOTABLE_PROOF_CANDIDATE",
    "MAKER_FILL_PROOF_CANDIDATE",
    "SPARSE_POSITIVE",
    "POSITIVE_EDGE_TOO_THIN",
    "POSITIVE_PF_WEAK",
    "FEE_WALL_NEAR_MISS",
    "VISUAL_EDGE_FEE_WALL",
    "PF_STRUCTURE_BUT_FEE_NEGATIVE",
    "OVERSCALP_FEE_BLEED",
    "NO_TRADES",
    "UNDER_SAMPLED_NEGATIVE",
    "BACKTEST_ERROR",
    "NEGATIVE_EDGE",
]


@dataclass(frozen=True)
class ScannerGateConfig:
    min_net_bps: float = 25.0
    min_profit_factor: float = 1.50
    min_trades: int = 20
    near_miss_net_floor_bps: float = -10.0
    visual_edge_floor_bps: float = 0.0

    def __post_init__(self) -> None:
        if self.min_net_bps <= 0:
            raise ValueError("min_net_bps must be positive")
        if self.min_profit_factor < 1.0:
            raise ValueError("min_profit_factor must be >= 1")
        if self.min_trades < 1:
            raise ValueError("min_trades must be >= 1")


@dataclass(frozen=True)
class ScannerEvidenceRow:
    evidence_source: str
    exchange: str
    symbol: str
    timeframe: str
    strategy_id: str
    mode: str
    samples: int
    avg_net_bps: float | None
    visual_avg_bps: float | None = None
    profit_factor: float | None = None
    win_rate_pct: float | None = None
    passed: bool = False
    actual_notional_avg: float | None = None
    margin_avg: float | None = None
    contracts_avg: float | None = None
    exits: dict[str, int] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ScannerUpliftRow:
    rank: int
    row_id: str
    failure_mode: FailureMode
    uplift_action: str
    use_as: str
    score: float
    required_uplift_bps: float | None
    fee_drag_bps: float | None
    exchange: str
    symbol: str
    timeframe: str
    strategy_id: str
    mode: str
    samples: int
    avg_net_bps: float | None
    visual_avg_bps: float | None
    profit_factor: float | None
    win_rate_pct: float | None
    passed: bool
    actual_notional_avg: float | None
    margin_avg: float | None
    contracts_avg: float | None
    exits: dict[str, int]
    rationale: str
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ScannerUpliftExperiment:
    experiment_id: str
    priority: int
    experiment_type: str
    target_rows: tuple[str, ...]
    exchange: str
    symbol: str
    timeframes: tuple[str, ...]
    strategy_id: str
    hypothesis: str
    required_change: str
    expected_effect: str
    guardrails: tuple[str, ...]
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def run_scanner_backtest_uplift(
    *,
    evidence_payloads: Iterable[dict],
    source_names: Iterable[str] | None = None,
    config: ScannerGateConfig = ScannerGateConfig(),
    max_rows: int = 80,
    max_experiments: int = 12,
    now: datetime | None = None,
) -> dict:
    generated = now or datetime.now(UTC)
    payloads = tuple(evidence_payloads)
    provided_sources = tuple(source_names or ())
    fallback_sources = tuple(
        f"payload_{idx}" for idx, _ in enumerate(payloads, start=1)
    )
    sources = tuple(
        provided_sources[idx] if idx < len(provided_sources) else fallback
        for idx, fallback in enumerate(fallback_sources)
    )
    rows = tuple(
        row
        for payload, source in zip(
            payloads,
            sources,
            strict=False,
        )
        for row in evidence_rows_from_payload(payload, evidence_source=source)
    )
    classified_rows = tuple(classify_evidence_row(row, config=config) for row in rows)
    uplift_rows = _rank_uplifts(classified_rows, max_rows=max_rows)
    scanner_families = _scanner_family_cards(rows, classified_rows, config=config)
    experiments = _build_experiments(uplift_rows, max_experiments=max_experiments)
    return {
        "agent_id": SCANNER_BACKTEST_UPLIFT_ID,
        "generated_at": generated.isoformat(),
        "summary": _summary(rows, classified_rows, uplift_rows, experiments, scanner_families, config),
        "policy": _policy(config),
        "top_uplifts": [row.to_dict() for row in uplift_rows],
        "scanner_families": scanner_families,
        "experiments": [row.to_dict() for row in experiments],
        "operator_answer": _operator_answer(uplift_rows, experiments, scanner_families),
        "can_trade": False,
        "can_promote": False,
    }


def evidence_rows_from_payload(
    payload: dict,
    *,
    evidence_source: str,
) -> tuple[ScannerEvidenceRow, ...]:
    if _is_second_eye_payload(payload):
        return tuple(_rows_from_second_eye_grid(payload, evidence_source))
    if isinstance(payload.get("rows"), list):
        return tuple(_row_from_pine_matrix(row, evidence_source) for row in payload["rows"])
    if isinstance(payload.get("candidates"), list):
        return tuple(
            _row_from_scanner_candidate(row, evidence_source)
            for row in payload["candidates"]
            if isinstance(row, dict)
        )
    if payload.get("truth_layer") == "vnedge_algo_ml_pro_pine_replay_v1":
        return (_row_from_pine_payload(payload, evidence_source),)
    return ()


def _is_second_eye_payload(payload: dict) -> bool:
    prereg = payload.get("pre_registry")
    if isinstance(prereg, dict) and prereg.get("registry_id") == "paper_only_survivor_prereg_v1":
        return True
    rows = payload.get("rows")
    return bool(
        isinstance(rows, list)
        and rows
        and isinstance(rows[0], dict)
        and ("taker" in rows[0] or "maker" in rows[0] or "strat" in rows[0])
    )


def _rows_from_second_eye_grid(
    payload: dict,
    evidence_source: str,
) -> tuple[ScannerEvidenceRow, ...]:
    rows: list[ScannerEvidenceRow] = []
    for source in payload.get("rows") or []:
        if isinstance(source, dict):
            rows.extend(_rows_from_second_eye_cell(source, evidence_source))
    return tuple(rows)


def _rows_from_second_eye_cell(
    row: dict,
    evidence_source: str,
) -> tuple[ScannerEvidenceRow, ...]:
    base = {
        "evidence_source": evidence_source,
        "exchange": str(row.get("exch") or row.get("exchange") or "unknown"),
        "symbol": str(row.get("sym") or row.get("symbol") or "unknown"),
        "timeframe": str(row.get("tf") or row.get("timeframe") or "unknown"),
        "strategy_id": str(row.get("strat") or row.get("strategy_id") or "unknown"),
        "raw": dict(row),
    }
    if row.get("error"):
        return (
            ScannerEvidenceRow(
                **base,
                mode="backtest_error",
                samples=0,
                avg_net_bps=None,
                profit_factor=None,
            ),
        )
    samples = _int(row.get("n"))
    if samples <= 0 or row.get("no_trade_sample"):
        return (
            ScannerEvidenceRow(
                **base,
                mode="no_trade",
                samples=0,
                avg_net_bps=None,
                profit_factor=None,
            ),
        )
    taker = _route(row.get("taker"))
    maker = _route(row.get("maker"))
    return (
        ScannerEvidenceRow(
            **base,
            mode="taker",
            samples=samples,
            avg_net_bps=taker.get("avg_net_bps"),
            visual_avg_bps=maker.get("avg_net_bps"),
            profit_factor=taker.get("pf"),
            win_rate_pct=taker.get("win"),
        ),
        ScannerEvidenceRow(
            **base,
            mode="maker_upper_bound",
            samples=samples,
            avg_net_bps=maker.get("avg_net_bps"),
            visual_avg_bps=taker.get("avg_net_bps"),
            profit_factor=maker.get("pf"),
            win_rate_pct=maker.get("win"),
        ),
    )


def classify_evidence_row(
    row: ScannerEvidenceRow,
    *,
    config: ScannerGateConfig = ScannerGateConfig(),
) -> ScannerUpliftRow:
    avg = row.avg_net_bps
    visual = row.visual_avg_bps
    pf = row.profit_factor
    fee_drag = _fee_drag_bps(visual, avg)
    required = None if avg is None else round(max(0.0, config.min_net_bps - avg), 4)
    failure = _failure_mode(row, config)
    action, use_as = _uplift_action(failure, row)
    return ScannerUpliftRow(
        rank=0,
        row_id=_row_id(row),
        failure_mode=failure,
        uplift_action=action,
        use_as=use_as,
        score=_score(row, failure, config),
        required_uplift_bps=required,
        fee_drag_bps=fee_drag,
        exchange=row.exchange,
        symbol=row.symbol,
        timeframe=row.timeframe,
        strategy_id=row.strategy_id,
        mode=row.mode,
        samples=row.samples,
        avg_net_bps=avg,
        visual_avg_bps=visual,
        profit_factor=pf,
        win_rate_pct=row.win_rate_pct,
        passed=row.passed,
        actual_notional_avg=row.actual_notional_avg,
        margin_avg=row.margin_avg,
        contracts_avg=row.contracts_avg,
        exits=dict(row.exits),
        rationale=_rationale(row, failure, required, fee_drag, config),
    )


def publish_scanner_backtest_uplift(
    payload: dict,
    *,
    out: Path | str = DEFAULT_OUT,
    feed: Path | str | None = DEFAULT_FEED,
) -> Path:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
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
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_feed_record(payload), sort_keys=True) + "\n")
        feed_path.chmod(0o644)
    return out_path


def _row_from_pine_matrix(row: dict, evidence_source: str) -> ScannerEvidenceRow:
    return ScannerEvidenceRow(
        evidence_source=evidence_source,
        exchange=str(row.get("exchange") or "delta_india"),
        symbol=str(row.get("symbol") or "unknown"),
        timeframe=str(row.get("timeframe") or "unknown"),
        strategy_id=str(row.get("strategy_id") or "vnedge_algo_ml_pro_v1"),
        mode=str(row.get("mode") or row.get("capture_mode") or "unknown"),
        samples=_int(row.get("closed") if row.get("closed") is not None else row.get("samples")),
        avg_net_bps=_float(row.get("fee_avg_bps") if row.get("fee_avg_bps") is not None else row.get("avg_net_bps")),
        visual_avg_bps=_float(row.get("visual_avg_bps")),
        profit_factor=_float(row.get("pf_r") if row.get("pf_r") is not None else row.get("profit_factor")),
        win_rate_pct=_float(row.get("win_rate_pct")),
        passed=bool(row.get("passed")),
        actual_notional_avg=_float(row.get("actual_notional_avg")),
        margin_avg=_float(row.get("margin_avg")),
        contracts_avg=_float(row.get("contracts_avg")),
        exits={str(k): _int(v) for k, v in dict(row.get("exits") or {}).items()},
        raw=dict(row),
    )


def _row_from_pine_payload(payload: dict, evidence_source: str) -> ScannerEvidenceRow:
    summary = dict(payload.get("summary") or {})
    sizing = dict(summary.get("position_sizing") or {})
    return ScannerEvidenceRow(
        evidence_source=evidence_source,
        exchange=str(payload.get("exchange") or "unknown"),
        symbol=str(payload.get("symbol") or "unknown"),
        timeframe=str(payload.get("timeframe") or "unknown"),
        strategy_id=str(payload.get("strategy_id") or "vnedge_algo_ml_pro_v1"),
        mode=str(payload.get("capture_mode") or "unknown"),
        samples=_int(summary.get("closed_trades")),
        avg_net_bps=_float(summary.get("fee_aware_avg_bps")),
        visual_avg_bps=_float(summary.get("visual_avg_bps")),
        profit_factor=_float(summary.get("profit_factor_r")),
        win_rate_pct=_float(summary.get("win_rate_pct")),
        passed=bool(dict(summary.get("promotion_gate") or {}).get("passed")),
        actual_notional_avg=_float(sizing.get("actual_notional_usd_avg")),
        margin_avg=_float(sizing.get("margin_usd_avg")),
        contracts_avg=_float(sizing.get("contracts_avg")),
        exits={str(k): _int(v) for k, v in dict(summary.get("exit_reason_counts") or {}).items()},
        raw=dict(payload),
    )


def _row_from_scanner_candidate(row: dict, evidence_source: str) -> ScannerEvidenceRow:
    return ScannerEvidenceRow(
        evidence_source=evidence_source,
        exchange=str(row.get("exchange") or "unknown"),
        symbol=str(row.get("symbol") or "unknown"),
        timeframe=str(row.get("timeframe") or "unknown"),
        strategy_id=str(row.get("strategy_id") or "unknown"),
        mode=str(row.get("dominant_route") or "edge_router"),
        samples=_int(row.get("routed")),
        avg_net_bps=_float(row.get("avg_selected_net_bps")),
        visual_avg_bps=_float(row.get("avg_selected_gross_bps")),
        profit_factor=_float(row.get("profit_factor")),
        win_rate_pct=_float(row.get("win_rate_pct")),
        passed=str(row.get("verdict") or "") == "STRICT_PROOF_WATCHLIST",
        exits={},
        raw=dict(row),
    )


def _failure_mode(row: ScannerEvidenceRow, config: ScannerGateConfig) -> FailureMode:
    avg = row.avg_net_bps
    visual = row.visual_avg_bps
    pf = row.profit_factor
    if row.raw.get("error") or row.mode == "backtest_error":
        return "BACKTEST_ERROR"
    if row.samples <= 0:
        return "NO_TRADES"
    route_row = row.mode in {"taker", "maker_upper_bound"}
    metric_gate_clears = (
        row.samples >= config.min_trades
        and avg is not None
        and avg >= config.min_net_bps
        and pf is not None
        and pf >= config.min_profit_factor
    )
    if metric_gate_clears and (row.passed or route_row):
        if row.mode == "maker_upper_bound":
            return "MAKER_FILL_PROOF_CANDIDATE"
        return "PROMOTABLE_PROOF_CANDIDATE"
    if row.samples < config.min_trades:
        return "SPARSE_POSITIVE" if avg is not None and avg > 0.0 else "UNDER_SAMPLED_NEGATIVE"
    if avg is not None and avg > 0.0:
        if pf is not None and pf < config.min_profit_factor:
            return "POSITIVE_PF_WEAK"
        return "POSITIVE_EDGE_TOO_THIN"
    if visual is not None and visual > config.visual_edge_floor_bps:
        if avg is not None and avg >= config.near_miss_net_floor_bps and (pf or 0.0) >= 1.0:
            return "FEE_WALL_NEAR_MISS"
        return "VISUAL_EDGE_FEE_WALL"
    if pf is not None and pf >= 1.20 and avg is not None and avg < 0.0:
        return "PF_STRUCTURE_BUT_FEE_NEGATIVE"
    if row.timeframe in {"1m", "5m"} and row.samples >= 100 and avg is not None and avg < 0.0:
        return "OVERSCALP_FEE_BLEED"
    return "NEGATIVE_EDGE"


def _uplift_action(failure: FailureMode, row: ScannerEvidenceRow) -> tuple[str, str]:
    if failure == "PROMOTABLE_PROOF_CANDIDATE":
        return "PRE_REGISTER_UNTOUCHED_JUDGMENT", "candidate_proof"
    if failure == "MAKER_FILL_PROOF_CANDIDATE":
        return "PROVE_MAKER_FILL_QUALITY_BEFORE_PAPER", "maker_probe"
    if failure == "SPARSE_POSITIVE":
        return "EXTEND_SAMPLE_ON_NEXT_UNTOUCHED_WINDOW", "sparse_candidate"
    if failure == "POSITIVE_EDGE_TOO_THIN":
        return "ADD_SELECTIVITY_NOT_FREQUENCY", "context_filter"
    if failure == "POSITIVE_PF_WEAK":
        return "REWORK_EXITS_AND_FALSE_POSITIVE_FILTER", "exit_lab"
    if failure == "FEE_WALL_NEAR_MISS":
        return "TEST_MAKER_FIRST_CONTEXT_FILTERED_ROUTE", "execution_uplift"
    if failure == "VISUAL_EDGE_FEE_WALL":
        return "RECYCLE_AS_FEATURE_AND_REQUIRE_COST_FORECAST", "feature_bank"
    if failure == "PF_STRUCTURE_BUT_FEE_NEGATIVE":
        return "MINE_WIN_CONTEXT_AND_ROUTE_SELECTIVELY", "edge_model_feature"
    if failure == "OVERSCALP_FEE_BLEED":
        return "BLOCK_CONTINUOUS_SCALP_REQUIRE_EVENT_CATALYST", "negative_training_label"
    if failure == "NO_TRADES":
        return "DROP_AS_SCALPER_OR_USE_AS_HTF_CONTEXT", "inactive_context"
    if failure == "UNDER_SAMPLED_NEGATIVE":
        return "COLLECT_MORE_DATA_ONLY_NO_TUNING", "weak_evidence"
    if failure == "BACKTEST_ERROR":
        return "REPAIR_BACKTEST_OR_DISABLE_SCANNER_FAMILY", "repair"
    return "REJECT_AS_STANDALONE_ENTRY", "negative_training_label"


def _rationale(
    row: ScannerEvidenceRow,
    failure: FailureMode,
    required_uplift_bps: float | None,
    fee_drag_bps: float | None,
    config: ScannerGateConfig,
) -> str:
    avg = "--" if row.avg_net_bps is None else f"{row.avg_net_bps:.2f}"
    pf = "--" if row.profit_factor is None else f"{row.profit_factor:.2f}"
    req = "--" if required_uplift_bps is None else f"{required_uplift_bps:.2f}"
    drag = "--" if fee_drag_bps is None else f"{fee_drag_bps:.2f}"
    if failure == "FEE_WALL_NEAR_MISS":
        return (
            f"{row.symbol} {row.timeframe} {row.mode} is close but still pays the "
            f"fee wall: avg {avg} bps, PF {pf}, fee drag {drag} bps. Needs about "
            f"{req} bps of extra net edge to reach the {config.min_net_bps:g} bps gate."
        )
    if failure == "POSITIVE_EDGE_TOO_THIN":
        return (
            f"Net result is positive but too thin: avg {avg} bps with "
            f"{row.samples} trades. Increase selectivity, not cadence."
        )
    if failure == "POSITIVE_PF_WEAK":
        return (
            f"Average is positive but PF {pf} is below {config.min_profit_factor:g}; "
            "the next research move is exit quality and false-positive pruning."
        )
    if failure == "SPARSE_POSITIVE":
        return (
            f"Positive but only {row.samples} trades; expand the untouched window "
            "instead of tuning on the seen slice."
        )
    if failure == "MAKER_FILL_PROOF_CANDIDATE":
        return (
            f"Maker economics clear gates on {row.symbol} {row.timeframe}, but this "
            "is an upper-bound route. Prove quote fill quality and adverse selection "
            "before paper observation."
        )
    if failure == "BACKTEST_ERROR":
        return (
            f"Backtest crashed for {row.strategy_id} on {row.symbol} {row.timeframe}: "
            f"{row.raw.get('error') or 'unknown error'}."
        )
    return (
        f"{failure.lower()} on {row.symbol} {row.timeframe}: avg {avg} bps, "
        f"PF {pf}, samples {row.samples}; row is useful as evidence but not promotion."
    )


def _rank_uplifts(
    rows: Iterable[ScannerUpliftRow],
    *,
    max_rows: int,
) -> tuple[ScannerUpliftRow, ...]:
    ranked = sorted(rows, key=lambda row: row.score, reverse=True)
    return tuple(
        ScannerUpliftRow(**{**asdict(row), "rank": rank})
        for rank, row in enumerate(ranked[:max_rows], start=1)
    )


def _build_experiments(
    rows: tuple[ScannerUpliftRow, ...],
    *,
    max_experiments: int,
) -> tuple[ScannerUpliftExperiment, ...]:
    groups: dict[tuple[str, str, str], list[ScannerUpliftRow]] = {}
    for row in rows:
        if row.failure_mode not in {
            "FEE_WALL_NEAR_MISS",
            "MAKER_FILL_PROOF_CANDIDATE",
            "POSITIVE_EDGE_TOO_THIN",
            "POSITIVE_PF_WEAK",
            "SPARSE_POSITIVE",
            "PF_STRUCTURE_BUT_FEE_NEGATIVE",
        }:
            continue
        groups.setdefault((row.exchange, row.symbol, row.strategy_id), []).append(row)

    experiments: list[ScannerUpliftExperiment] = []
    for index, ((exchange, symbol, strategy), group) in enumerate(
        sorted(groups.items(), key=lambda item: max(row.score for row in item[1]), reverse=True),
        start=1,
    ):
        top = sorted(group, key=lambda row: row.score, reverse=True)[:4]
        modes = {row.failure_mode for row in top}
        if "FEE_WALL_NEAR_MISS" in modes:
            exp_type = "maker_first_context_filtered_replay"
            change = "Require HTF bias, BBP/ADX alignment, volume impulse, and maker-first route before allowing taker fallback."
            effect = "Cut fee drag and reject visual-only entries that do not forecast >25 bps net."
        elif "MAKER_FILL_PROOF_CANDIDATE" in modes:
            exp_type = "maker_fill_quality_probe"
            change = "Replay strict quote placement with fill probability, adverse selection, cancel TTL, and taker fallback only above the fee buffer."
            effect = "Separate real maker edge from optimistic backtest economics."
        elif "POSITIVE_PF_WEAK" in modes:
            exp_type = "exit_overlay_replay"
            change = "Test faster invalidation, BE after TP1, and trail tightening against the same entry timestamps."
            effect = "Raise PF by shrinking tail losses without adding more trades."
        elif "SPARSE_POSITIVE" in modes:
            exp_type = "sample_expansion"
            change = "Run the same frozen setup on a longer untouched window and cross-venue sample."
            effect = "Decide whether sparse positives are stable or random."
        else:
            exp_type = "selectivity_filter_replay"
            change = "Mine winning contexts from the row set and add only causal pre-entry filters."
            effect = "Raise average net bps by trading fewer but stronger setups."
        experiments.append(
            ScannerUpliftExperiment(
                experiment_id=f"{exp_type}|{exchange}|{symbol}|{strategy}",
                priority=index,
                experiment_type=exp_type,
                target_rows=tuple(row.row_id for row in top),
                exchange=exchange,
                symbol=symbol,
                timeframes=tuple(dict.fromkeys(row.timeframe for row in top)),
                strategy_id=strategy,
                hypothesis=_hypothesis(top),
                required_change=change,
                expected_effect=effect,
                guardrails=_guardrails(),
            )
        )
        if len(experiments) >= max_experiments:
            break
    return tuple(experiments)


def _summary(
    rows: tuple[ScannerEvidenceRow, ...],
    classified_rows: tuple[ScannerUpliftRow, ...],
    uplift_rows: tuple[ScannerUpliftRow, ...],
    experiments: tuple[ScannerUpliftExperiment, ...],
    scanner_families: tuple[dict, ...],
    config: ScannerGateConfig,
) -> dict:
    modes = Counter(row.failure_mode for row in classified_rows)
    family_verdicts = Counter(str(row.get("family_verdict") or "") for row in scanner_families)
    positive_after_cost = [row for row in classified_rows if (row.avg_net_bps or 0.0) > 0.0]
    visual_only = [
        row for row in classified_rows
        if (row.visual_avg_bps or 0.0) > 0.0 and (row.avg_net_bps or 0.0) <= 0.0
    ]
    near = [row for row in classified_rows if row.failure_mode == "FEE_WALL_NEAR_MISS"]
    promotable = [
        row for row in classified_rows
        if row.failure_mode == "PROMOTABLE_PROOF_CANDIDATE"
    ]
    best = uplift_rows[0] if uplift_rows else None
    return {
        "evidence_rows": len(rows),
        "ranked_rows": len(uplift_rows),
        "registered_scanners": len(STRATEGIES),
        "scanner_families": len(scanner_families),
        "families_with_strict_proof": family_verdicts["FAMILY_STRICT_SURVIVOR"],
        "families_with_maker_probe": family_verdicts["FAMILY_MAKER_PROBE"],
        "families_needing_repair": family_verdicts["FAMILY_REPAIR_BACKTEST"],
        "families_pending_grid": family_verdicts["FAMILY_PENDING_GRID"],
        "families_quarantined": family_verdicts["FAMILY_QUARANTINE"],
        "family_verdicts": dict(family_verdicts),
        "promotable_proof_candidates": len(promotable),
        "positive_after_cost": len(positive_after_cost),
        "visual_only_positive": len(visual_only),
        "fee_wall_near_misses": len(near),
        "experiments": len(experiments),
        "failure_modes": dict(modes),
        "gate": asdict(config),
        "best_row_id": best.row_id if best is not None else None,
        "best_failure_mode": best.failure_mode if best is not None else None,
        "best_avg_net_bps": best.avg_net_bps if best is not None else None,
        "best_profit_factor": best.profit_factor if best is not None else None,
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
    }


def _scanner_family_cards(
    evidence_rows: tuple[ScannerEvidenceRow, ...],
    classified_rows: tuple[ScannerUpliftRow, ...],
    *,
    config: ScannerGateConfig,
) -> tuple[dict, ...]:
    by_strategy: dict[str, list[ScannerUpliftRow]] = {}
    evidence_cells: dict[str, set[tuple[str, str, str, str]]] = {}
    for row in classified_rows:
        by_strategy.setdefault(row.strategy_id, []).append(row)
        evidence_cells.setdefault(row.strategy_id, set()).add(
            (row.exchange, row.symbol, row.timeframe, row.mode)
        )
    for row in evidence_rows:
        by_strategy.setdefault(row.strategy_id, [])
        evidence_cells.setdefault(row.strategy_id, set()).add(
            (row.exchange, row.symbol, row.timeframe, row.mode)
        )

    registered = tuple(STRATEGIES.keys())
    discovered = tuple(sorted(set(by_strategy) - set(registered)))
    strategy_ids = registered + discovered
    cards = [
        _scanner_family_card(
            strategy_id,
            rows=tuple(by_strategy.get(strategy_id, ())),
            evidence_cells=evidence_cells.get(strategy_id, set()),
            config=config,
        )
        for strategy_id in strategy_ids
    ]
    return tuple(sorted(cards, key=_family_sort_key))


def _scanner_family_card(
    strategy_id: str,
    *,
    rows: tuple[ScannerUpliftRow, ...],
    evidence_cells: set[tuple[str, str, str, str]],
    config: ScannerGateConfig,
) -> dict:
    modes = Counter(row.failure_mode for row in rows)
    best = max(rows, key=lambda row: row.score, default=None)
    verdict, action, use_as = _family_verdict(modes=modes, has_rows=bool(rows))
    return {
        "strategy_id": strategy_id,
        "module": getattr(STRATEGIES.get(strategy_id), "__module__", ""),
        "family_verdict": verdict,
        "uplift_action": action,
        "use_as": use_as,
        "score": _family_score(best, verdict),
        "evidence_cells": len(evidence_cells),
        "route_rows": len(rows),
        "failure_modes": dict(sorted(modes.items())),
        "best_cell": _family_best_cell(best),
        "flaws": _family_flaws(verdict, modes),
        "improvements": _family_improvements(verdict, modes, config),
        "guardrails": _guardrails(),
        "can_trade": False,
        "can_promote": False,
    }


def _family_verdict(
    *,
    modes: Counter[str],
    has_rows: bool,
) -> tuple[str, str, str]:
    if not has_rows:
        return "FAMILY_PENDING_GRID", "WAIT_FOR_SECOND_EYE_GRID", "pending"
    if modes["PROMOTABLE_PROOF_CANDIDATE"]:
        return (
            "FAMILY_STRICT_SURVIVOR",
            "FREEZE_WINNING_ROUTE_AND_PREPARE_PAPER_ONLY_REVIEW",
            "paper_candidate",
        )
    if modes["MAKER_FILL_PROOF_CANDIDATE"]:
        return (
            "FAMILY_MAKER_PROBE",
            "RUN_MAKER_FILL_QUALITY_PROBE_BEFORE_PAPER",
            "maker_probe",
        )
    if modes["BACKTEST_ERROR"] and sum(modes.values()) == modes["BACKTEST_ERROR"]:
        return (
            "FAMILY_REPAIR_BACKTEST",
            "FIX_SCANNER_DATA_CONTRACT_OR_DISABLE",
            "repair",
        )
    if modes["SPARSE_POSITIVE"]:
        return (
            "FAMILY_SPARSE_EXTEND",
            "EXTEND_SAMPLE_ON_NEXT_UNTOUCHED_WINDOW",
            "sparse_candidate",
        )
    if modes["FEE_WALL_NEAR_MISS"] or modes["POSITIVE_EDGE_TOO_THIN"] or modes["POSITIVE_PF_WEAK"]:
        return (
            "FAMILY_UPLIFT_REQUIRED",
            "ADD_CAUSAL_SELECTIVITY_EXIT_OR_ROUTE_FILTER",
            "research_uplift",
        )
    if modes["NO_TRADES"] and sum(modes.values()) == modes["NO_TRADES"]:
        return (
            "FAMILY_NO_TRADES",
            "REMOVE_FROM_PAPER_AND_KEEP_AS_CONTEXT_ONLY",
            "context_only",
        )
    return "FAMILY_QUARANTINE", "QUARANTINE_AS_STANDALONE_ENTRY", "negative_label"


def _family_best_cell(row: ScannerUpliftRow | None) -> dict:
    if row is None:
        return {}
    return {
        "row_id": row.row_id,
        "exchange": row.exchange,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "mode": row.mode,
        "failure_mode": row.failure_mode,
        "samples": row.samples,
        "avg_net_bps": row.avg_net_bps,
        "profit_factor": row.profit_factor,
        "required_uplift_bps": row.required_uplift_bps,
        "uplift_action": row.uplift_action,
    }


def _family_flaws(verdict: str, modes: Counter[str]) -> list[str]:
    if verdict == "FAMILY_PENDING_GRID":
        return ["second_eye_grid_has_not_reached_this_scanner_yet"]
    if verdict == "FAMILY_STRICT_SURVIVOR":
        return ["single_window_evidence_only", "needs_untouched_walk_forward_before_promotion"]
    if verdict == "FAMILY_MAKER_PROBE":
        return ["maker_fill_assumption_unproven", "possible_adverse_selection"]
    if verdict == "FAMILY_REPAIR_BACKTEST":
        return ["scanner_or_data_contract_crashes_in_grid"]
    if verdict == "FAMILY_NO_TRADES":
        return ["no_trade_cells_across_observed_universe"]
    flaws: list[str] = []
    if modes["FEE_WALL_NEAR_MISS"] or modes["VISUAL_EDGE_FEE_WALL"]:
        flaws.append("visual_edge_does_not_survive_fees")
    if modes["POSITIVE_PF_WEAK"]:
        flaws.append("false_positive_or_exit_tail_problem")
    if modes["OVERSCALP_FEE_BLEED"]:
        flaws.append("too_many_low_edge_short_tf_entries")
    if modes["NEGATIVE_EDGE"] or modes["UNDER_SAMPLED_NEGATIVE"]:
        flaws.append("standalone_entry_negative_after_costs")
    return flaws or ["no_promotable_structure_found_yet"]


def _family_improvements(
    verdict: str,
    modes: Counter[str],
    config: ScannerGateConfig,
) -> list[str]:
    if verdict == "FAMILY_STRICT_SURVIVOR":
        return [
            "freeze_best_route_params_before_any_paper_change",
            "run_next_untouched_walk_forward_judgment",
            "paper_only_forward_observation_after_grid_complete",
        ]
    if verdict == "FAMILY_MAKER_PROBE":
        return [
            "measure_limit_order_fill_rate_and_adverse_selection",
            "allow_taker_fallback_only_when_expected_move_clears_fee_slippage_buffer",
            "cap_probe_count_until_fill_quality_is_real",
        ]
    if verdict == "FAMILY_SPARSE_EXTEND":
        return [
            "extend_sample_on_next_untouched_window",
            "do_not_tune_sparse_positive_seen_data",
        ]
    if verdict == "FAMILY_REPAIR_BACKTEST":
        return [
            "make_prepare_signal_exit_contract_backtest_safe",
            "mark_unsupported_data_dependencies_explicitly",
        ]
    if verdict == "FAMILY_NO_TRADES":
        return [
            "remove_from_paper_roster",
            "reuse_only_as_higher_timeframe_context_feature",
        ]
    improvements = [
        "increase_selectivity_not_trade_frequency",
        f"require_expected_net_edge_above_{config.min_net_bps:g}_bps",
        "test_active_exit_overlay_without_changing_entry_timestamps",
    ]
    if modes["FEE_WALL_NEAR_MISS"] or modes["VISUAL_EDGE_FEE_WALL"]:
        improvements.append("test_maker_first_route_with_real_fill_quality")
    if modes["PF_STRUCTURE_BUT_FEE_NEGATIVE"]:
        improvements.append("mine_winning_context_as_edge_model_feature")
    if modes["POSITIVE_PF_WEAK"]:
        improvements.append("add_false_positive_filter_and_profit_lock_exit")
    return improvements


def _family_score(row: ScannerUpliftRow | None, verdict: str) -> float:
    if row is None:
        return 0.0
    bonus = {
        "FAMILY_STRICT_SURVIVOR": 25.0,
        "FAMILY_MAKER_PROBE": 15.0,
        "FAMILY_SPARSE_EXTEND": 7.0,
        "FAMILY_UPLIFT_REQUIRED": 3.0,
        "FAMILY_REPAIR_BACKTEST": -20.0,
        "FAMILY_NO_TRADES": -25.0,
        "FAMILY_QUARANTINE": -30.0,
    }.get(verdict, 0.0)
    return round(max(0.0, row.score + bonus), 4)


def _family_sort_key(row: dict) -> tuple[int, float, str]:
    priority = {
        "FAMILY_STRICT_SURVIVOR": 0,
        "FAMILY_MAKER_PROBE": 1,
        "FAMILY_UPLIFT_REQUIRED": 2,
        "FAMILY_SPARSE_EXTEND": 3,
        "FAMILY_REPAIR_BACKTEST": 4,
        "FAMILY_PENDING_GRID": 5,
        "FAMILY_NO_TRADES": 6,
        "FAMILY_QUARANTINE": 7,
    }.get(str(row.get("family_verdict") or ""), 9)
    return (priority, -_float_or_zero(row.get("score")), str(row.get("strategy_id") or ""))


def _operator_answer(
    rows: tuple[ScannerUpliftRow, ...],
    experiments: tuple[ScannerUpliftExperiment, ...],
    scanner_families: tuple[dict, ...],
) -> str:
    strict_families = [
        row for row in scanner_families if row.get("family_verdict") == "FAMILY_STRICT_SURVIVOR"
    ]
    maker_families = [
        row for row in scanner_families if row.get("family_verdict") == "FAMILY_MAKER_PROBE"
    ]
    if strict_families or maker_families:
        return (
            f"Scanner-family study found {len(strict_families)} strict family/families "
            f"and {len(maker_families)} maker-probe family/families. Keep everything "
            "paper-only until the full grid completes and fill quality is proven."
        )
    if not rows:
        return "No scanner backtest evidence was available for uplift analysis."
    best = rows[0]
    if best.failure_mode == "PROMOTABLE_PROOF_CANDIDATE":
        return (
            f"Best row is {best.row_id} and clears proof gates on this evidence; "
            "next step is an untouched-window judgment, not live promotion."
        )
    if best.failure_mode in {"FEE_WALL_NEAR_MISS", "POSITIVE_EDGE_TOO_THIN", "POSITIVE_PF_WEAK"}:
        return (
            f"Best row is {best.row_id}: {best.failure_mode}. It is not paper-ready, "
            f"but it gives a concrete uplift target. {len(experiments)} research "
            "experiments are queued around maker-first routing, context filters, and exits."
        )
    return (
        f"Best row is {best.row_id}: {best.failure_mode}. The current scanner evidence "
        "does not break the fee wall; use it as training/failure context before adding trades."
    )


def _policy(config: ScannerGateConfig) -> dict:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "requires_untouched_judgment": True,
        "min_net_bps": config.min_net_bps,
        "min_profit_factor": config.min_profit_factor,
        "min_trades": config.min_trades,
        "operator_note": (
            "This report mines failed and near-miss scanner backtests. It may create "
            "new research experiments, but it cannot lower live gates or promote lanes."
        ),
    }


def _score(row: ScannerEvidenceRow, failure: FailureMode, config: ScannerGateConfig) -> float:
    avg = row.avg_net_bps if row.avg_net_bps is not None else -50.0
    visual = row.visual_avg_bps if row.visual_avg_bps is not None else avg
    pf = min(row.profit_factor if row.profit_factor is not None else 0.0, 5.0)
    sample_bonus = min(math.sqrt(max(row.samples, 0)), 12.0)
    mode_bonus = {
        "PROMOTABLE_PROOF_CANDIDATE": 100.0,
        "MAKER_FILL_PROOF_CANDIDATE": 70.0,
        "FEE_WALL_NEAR_MISS": 45.0,
        "POSITIVE_EDGE_TOO_THIN": 40.0,
        "POSITIVE_PF_WEAK": 35.0,
        "SPARSE_POSITIVE": 25.0,
        "PF_STRUCTURE_BUT_FEE_NEGATIVE": 18.0,
        "VISUAL_EDGE_FEE_WALL": 10.0,
        "OVERSCALP_FEE_BLEED": -15.0,
        "NO_TRADES": -40.0,
        "UNDER_SAMPLED_NEGATIVE": -30.0,
        "BACKTEST_ERROR": -45.0,
        "NEGATIVE_EDGE": -25.0,
    }[failure]
    uplift_penalty = max(0.0, config.min_net_bps - avg) * 0.35
    return round(avg + max(visual, 0.0) * 0.20 + pf * 6.0 + sample_bonus + mode_bonus - uplift_penalty, 4)


def _hypothesis(rows: Iterable[ScannerUpliftRow]) -> str:
    rows = tuple(rows)
    best = rows[0]
    return (
        f"{best.strategy_id} on {best.symbol} has structure but not enough net edge; "
        "a causal filter or route overlay should keep the high-MFE contexts and skip fee-wall churn."
    )


def _guardrails() -> tuple[str, ...]:
    return (
        "research-only output; no paper/live promotion",
        "all filters must use pre-entry causal features only",
        "do not tune on a judged window",
        "taker fallback must clear fees, slippage, and safety buffer",
        "new pass still requires untouched-window judgment",
    )


def _row_id(row: ScannerEvidenceRow) -> str:
    symbol = row.symbol.replace("/", "").replace(":", "").replace("-", "")
    return f"{row.strategy_id}|{row.exchange}|{symbol}|{row.timeframe}|{row.mode}"


def _feed_record(payload: dict) -> dict:
    return {
        "agent_id": payload.get("agent_id"),
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary", {}),
        "operator_answer": payload.get("operator_answer"),
        "can_trade": False,
        "can_promote": False,
    }


def _fee_drag_bps(visual: float | None, avg: float | None) -> float | None:
    if visual is None or avg is None:
        return None
    value = visual - avg
    return round(value, 4) if math.isfinite(value) else None


def _route(value: object) -> dict[str, float | None]:
    row = value if isinstance(value, dict) else {}
    return {
        "net": _float(row.get("net")),
        "pf": _float(row.get("pf")),
        "win": _float(row.get("win")),
        "dd": _float(row.get("dd")),
        "avg_net_bps": _float(row.get("avg_net_bps")),
    }


def _float_or_zero(value: object) -> float:
    out = _float(value)
    return out if out is not None else 0.0


def _float(value: object) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value: object) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return 0
    return out


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {"rows": data}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="research-only scanner backtest uplift")
    parser.add_argument("--input", action="append", required=True, help="JSON evidence payload; repeatable")
    parser.add_argument("--source-name", action="append", help="Optional source label matching --input")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-rows", type=int, default=80)
    parser.add_argument("--max-experiments", type=int, default=12)
    parser.add_argument("--min-net-bps", type=float, default=25.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.50)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="repeat forever at this cadence; 0 runs once",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    while True:
        payloads = tuple(_read_json(Path(path)) for path in args.input)
        source_names = tuple(args.source_name or ()) or tuple(Path(path).name for path in args.input)
        report = run_scanner_backtest_uplift(
            evidence_payloads=payloads,
            source_names=source_names,
            config=ScannerGateConfig(
                min_net_bps=args.min_net_bps,
                min_profit_factor=args.min_profit_factor,
                min_trades=args.min_trades,
            ),
            max_rows=args.max_rows,
            max_experiments=args.max_experiments,
        )
        path = publish_scanner_backtest_uplift(
            report,
            out=args.out,
            feed=None if args.feed == "" else args.feed,
        )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True), flush=True)
        else:
            print(f"scanner backtest uplift wrote {path}", flush=True)
            print(report["operator_answer"], flush=True)
        if args.interval_seconds <= 0:
            break
        time.sleep(max(1, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
