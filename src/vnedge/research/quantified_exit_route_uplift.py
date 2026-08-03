"""Exit/route uplift experiments for Quantified Strategy Lab near-misses.

The proof arbiter tells us which Quantified blueprint cells are positive but
too thin to clear the fee wall.  This publisher turns those cells into a
durable Agent Gateway experiment queue:

* taker-cost TP ladder + active breakeven/trail parity;
* simple profit-lock/breakeven rescue;
* maker-entry/taker-exit route model, explicitly labelled as an upper-bound
  until L2 fill proof validates resting order fills.

Research-only invariants stay intact: this module never promotes, never writes
paper/live lane manifests, and every job/result is stamped non-trading.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Any, Mapping

from vnedge.agent_gateway.jobs import (
    BLOCKED_STATUS,
    DONE_STATUS,
    FAILED_STATUS,
    PENDING_STATUS,
    RUNNING_STATUS,
    create_backtest_job,
)
from vnedge.research.quantified_blueprint_proof import (
    DEFAULT_ARTIFACT_DIR,
    DEFAULT_GATE_NET_BPS,
    DEFAULT_GATE_PF,
    DEFAULT_GATE_TRADES,
    QuantifiedBlueprintProofConfig,
    default_profiles,
)
from vnedge.research.quantified_proof_result_arbiter import (
    DEFAULT_OUT as DEFAULT_ARBITER,
)


QUANTIFIED_EXIT_ROUTE_UPLIFT_ID = "quantified_exit_route_uplift_v1"
DEFAULT_OUT = Path("research/live_research/quantified_exit_route_uplift_latest.json")
DEFAULT_FEED = Path("research/live_research/quantified_exit_route_uplift_feed.jsonl")
STATUS_QUEUED = "QUEUED_RESEARCH_ONLY"
UPLIFT_BUCKETS = frozenset({"EXIT_ROUTE_UPLIFT", "FEE_WALL_NEAR_MISS"})


def env_agent_jobs_dir(env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("AGENT_GATEWAY_JOBS_DIR", "logs/agent_gateway/jobs"))


@dataclass(frozen=True)
class ExitRouteVariant:
    variant_id: str
    label: str
    route_model: str
    route_assumption: str
    config_parameters: Mapping[str, Any]
    strategy_parameter_overrides: Mapping[str, Any]
    maker_fill_modelled: bool = False


@dataclass(frozen=True)
class QuantifiedExitRouteUpliftConfig:
    min_net_bps: float = DEFAULT_GATE_NET_BPS
    min_profit_factor: float = DEFAULT_GATE_PF
    min_trades: int = DEFAULT_GATE_TRADES
    max_source_actions: int = 40
    initial_capital_usd: float = 100.0
    paper_margin_usd: float = 100.0
    paper_leverage: float = 25.0
    variants: tuple[ExitRouteVariant, ...] = ()

    def __post_init__(self) -> None:
        if self.min_net_bps <= 0:
            raise ValueError("min_net_bps must be positive")
        if self.min_profit_factor < 1:
            raise ValueError("min_profit_factor must be >= 1")
        if self.min_trades < 1:
            raise ValueError("min_trades must be positive")
        if self.max_source_actions < 1:
            raise ValueError("max_source_actions must be positive")
        if self.initial_capital_usd <= 0 or self.paper_margin_usd <= 0:
            raise ValueError("capital/margin must be positive")
        if self.paper_leverage <= 0:
            raise ValueError("paper_leverage must be positive")

    @property
    def paper_notional_usd(self) -> float:
        return self.paper_margin_usd * self.paper_leverage

    @property
    def active_variants(self) -> tuple[ExitRouteVariant, ...]:
        return self.variants or default_variants()


def default_variants() -> tuple[ExitRouteVariant, ...]:
    return (
        ExitRouteVariant(
            variant_id="tp1_be_trail_taker_v1",
            label="TP1/TP2 partials + fee-aware BE + ATR trail, taker-cost baseline",
            route_model="taker_entry_taker_exit",
            route_assumption="runtime_active_exit_parity_no_maker_fill_assumption",
            config_parameters={
                "use_active_exit": True,
                "trail_atr_mult": 2.0,
                "trail_atr_window": 14,
            },
            strategy_parameter_overrides={
                "emit_tp_ladder": True,
                "tp1_r": 0.80,
                "tp2_r": 1.50,
            },
        ),
        ExitRouteVariant(
            variant_id="profit_lock_20_10_taker_v1",
            label="Arm at +20 bps, lock +10 bps, taker-cost baseline",
            route_model="taker_entry_taker_exit",
            route_assumption="bar_close_profit_lock_no_maker_fill_assumption",
            config_parameters={
                "breakeven_arm_bps": 20.0,
                "profit_lock_bps": 10.0,
            },
            strategy_parameter_overrides={},
        ),
        ExitRouteVariant(
            variant_id="maker_entry_tp1_be_trail_v1",
            label="Maker-entry/taker-exit upper-bound with active exit",
            route_model="maker_entry_taker_exit",
            route_assumption=(
                "candle_backtest_models_resting_entry_fill; must clear L2 "
                "trade-through fill proof before promotion"
            ),
            config_parameters={
                "use_active_exit": True,
                "trail_atr_mult": 2.0,
                "trail_atr_window": 14,
                "entry_fee_bps": 2.0,
                "exit_fee_bps": 5.0,
                "entry_slippage_bps": 0.5,
                "exit_slippage_bps": 2.0,
            },
            strategy_parameter_overrides={
                "emit_tp_ladder": True,
                "tp1_r": 0.80,
                "tp2_r": 1.50,
            },
            maker_fill_modelled=True,
        ),
    )


def build_quantified_exit_route_uplift_payload(
    *,
    arbiter_payload: Mapping[str, Any] | None = None,
    arbiter_path: Path | str | None = DEFAULT_ARBITER,
    jobs_dir: Path | str | None = None,
    artifact_dir: Path | str | None = DEFAULT_ARTIFACT_DIR,
    config: QuantifiedExitRouteUpliftConfig | None = None,
    seed_jobs: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    cfg = config or _config_from_arbiter(arbiter_payload, arbiter_path)
    generated = now or datetime.now(UTC)
    arbiter = (
        dict(arbiter_payload)
        if arbiter_payload is not None
        else _read_json(Path(arbiter_path)) if arbiter_path is not None else {}
    )
    source_actions = _source_actions(arbiter, cfg)
    specs = _experiment_specs(source_actions, cfg)
    jobs_path = Path(jobs_dir) if jobs_dir is not None else env_agent_jobs_dir()
    jobs_before = _jobs_by_hypothesis(jobs_path, {str(spec["hypothesis_id"]) for spec in specs})
    created = 0
    if seed_jobs:
        for spec in specs:
            if spec["hypothesis_id"] in jobs_before:
                continue
            create_backtest_job(
                jobs_dir=jobs_path,
                agent=QUANTIFIED_EXIT_ROUTE_UPLIFT_ID,
                request=_request_for_spec(spec, cfg),
            )
            created += 1
        jobs = _jobs_by_hypothesis(jobs_path, {str(spec["hypothesis_id"]) for spec in specs})
    else:
        jobs = jobs_before

    rows = [
        _row_for_spec(
            spec,
            jobs.get(str(spec["hypothesis_id"])),
            config=cfg,
            artifact_dir=Path(artifact_dir) if artifact_dir is not None else None,
        )
        for spec in specs
    ]
    summary = _summary(source_actions, rows, created=created, config=cfg)
    return {
        "uplift_id": QUANTIFIED_EXIT_ROUTE_UPLIFT_ID,
        "generated_at": generated.isoformat(),
        "source": {
            "arbiter_id": arbiter.get("arbiter_id") or "missing",
            "arbiter_generated_at": arbiter.get("generated_at"),
            "arbiter_path": str(arbiter_path or "inline_payload"),
        },
        "mode": "research_only_exit_route_agent_gateway_backtest_queue",
        "config": {
            **asdict(cfg),
            "variants": [asdict(variant) for variant in cfg.active_variants],
        },
        "summary": summary,
        "source_actions": source_actions,
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": _policy(),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def publish_quantified_exit_route_uplift(
    payload: dict[str, Any],
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


def load_quantified_exit_route_uplift_payload(path: Path | None = None) -> dict[str, Any]:
    if path is not None and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if (
            isinstance(payload, dict)
            and payload.get("uplift_id") == QUANTIFIED_EXIT_ROUTE_UPLIFT_ID
        ):
            return payload
    return build_quantified_exit_route_uplift_payload(seed_jobs=False)


def _source_actions(
    arbiter: Mapping[str, Any],
    config: QuantifiedExitRouteUpliftConfig,
) -> list[dict[str, Any]]:
    raw = arbiter.get("action_queue")
    actions = [dict(row) for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    filtered = [
        action
        for action in actions
        if str(action.get("bucket") or "") in UPLIFT_BUCKETS
        and str(action.get("status") or "") == DONE_STATUS
        and action.get("avg_net_bps") is not None
    ]
    filtered.sort(
        key=lambda row: (
            _float(row.get("avg_net_bps")) or -1e9,
            _float(row.get("profit_factor")) or -1e9,
            _int(row.get("samples")),
        ),
        reverse=True,
    )
    return filtered[: config.max_source_actions]


def _experiment_specs(
    source_actions: list[dict[str, Any]],
    config: QuantifiedExitRouteUpliftConfig,
) -> list[dict[str, Any]]:
    profiles = {profile.port_id: profile for profile in default_profiles()}
    specs: list[dict[str, Any]] = []
    for action in source_actions:
        profile = profiles.get(str(action.get("port_id") or ""))
        if profile is None:
            continue
        for variant in config.active_variants:
            specs.append(
                {
                    "hypothesis_id": _hypothesis_id(action, variant),
                    "source_action_id": str(action.get("action_id") or ""),
                    "source_bucket": str(action.get("bucket") or ""),
                    "source_avg_net_bps": _float(action.get("avg_net_bps")),
                    "source_profit_factor": _float(action.get("profit_factor")),
                    "source_samples": _int(action.get("samples")),
                    "required_uplift_bps": _float(action.get("required_uplift_bps")),
                    "variant": variant,
                    "port_id": profile.port_id,
                    "strategy_id": profile.strategy_id,
                    "adapter": profile.adapter,
                    "setup_mode": profile.setup_mode,
                    "canonical_adapter": bool(action.get("canonical_adapter", profile.canonical_adapter)),
                    "exchange": str(action.get("exchange") or ""),
                    "symbol": str(action.get("symbol") or ""),
                    "timeframe": str(action.get("timeframe") or ""),
                    "max_holding_bars": _int(action.get("max_holding_bars"))
                    or _holding_bars(str(action.get("timeframe") or "")),
                    "strategy_parameters": _strategy_parameters(
                        profile.strategy_parameters,
                        variant,
                        strategy_id=profile.strategy_id,
                    ),
                }
            )
    return specs


def _strategy_parameters(
    base: Mapping[str, Any],
    variant: ExitRouteVariant,
    *,
    strategy_id: str,
) -> dict[str, Any]:
    params = dict(base)
    # Fee-wall sniper already emits a TP ladder. The override is only needed for
    # Quant Signal Pack, which historically emitted a single TP unless asked.
    if strategy_id != "quant_signal_pack_v1":
        return params
    nested = params.get("params")
    if isinstance(nested, dict):
        params["params"] = {**nested, **dict(variant.strategy_parameter_overrides)}
    else:
        params.update(dict(variant.strategy_parameter_overrides))
    return params


def _hypothesis_id(action: Mapping[str, Any], variant: ExitRouteVariant) -> str:
    symbol = str(action.get("symbol") or "symbol").replace("/", "").replace(":", "").lower()
    return "|".join(
        (
            QUANTIFIED_EXIT_ROUTE_UPLIFT_ID,
            str(action.get("port_id") or "port"),
            str(action.get("exchange") or "exchange"),
            symbol,
            str(action.get("timeframe") or "tf"),
            variant.variant_id,
        )
    )


def _request_for_spec(
    spec: Mapping[str, Any],
    config: QuantifiedExitRouteUpliftConfig,
) -> dict[str, Any]:
    variant: ExitRouteVariant = spec["variant"]
    parameters = {
        "hypothesis_id": spec["hypothesis_id"],
        "source_action_id": spec["source_action_id"],
        "source_avg_net_bps": spec["source_avg_net_bps"],
        "source_profit_factor": spec["source_profit_factor"],
        "source_samples": spec["source_samples"],
        "required_uplift_bps": spec["required_uplift_bps"],
        "port_id": spec["port_id"],
        "proof_id": QUANTIFIED_EXIT_ROUTE_UPLIFT_ID,
        "adapter": spec["adapter"],
        "setup_mode": spec["setup_mode"],
        "canonical_adapter": bool(spec["canonical_adapter"]),
        "max_holding_bars": spec["max_holding_bars"],
        "paper_margin_usd": config.paper_margin_usd,
        "paper_leverage": config.paper_leverage,
        "paper_notional_usd": config.paper_notional_usd,
        "uplift_variant": variant.variant_id,
        "execution_route_model": variant.route_model,
        "route_assumption": variant.route_assumption,
        **dict(spec.get("strategy_parameters") or {}),
        **dict(variant.config_parameters),
    }
    return {
        "strategy_id": spec["strategy_id"],
        "exchange": spec["exchange"],
        "symbol": spec["symbol"],
        "timeframe": spec["timeframe"],
        "initial_capital_usd": config.initial_capital_usd,
        "commission_bps": None,
        "slippage_bps": None,
        "strict_mode": True,
        "live_orders_enabled": False,
        "parameters": parameters,
    }


def _jobs_by_hypothesis(jobs_dir: Path, desired: set[str]) -> dict[str, dict[str, Any]]:
    if not jobs_dir.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(jobs_dir.glob("agj_*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(job, dict):
            continue
        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        params = request.get("parameters") if isinstance(request.get("parameters"), dict) else {}
        hypothesis_id = str(params.get("hypothesis_id") or "")
        if hypothesis_id not in desired:
            continue
        current = out.get(hypothesis_id)
        if current is None or str(job.get("updated_at") or "") >= str(current.get("updated_at") or ""):
            out[hypothesis_id] = job
    return out


def _row_for_spec(
    spec: Mapping[str, Any],
    job: dict[str, Any] | None,
    *,
    config: QuantifiedExitRouteUpliftConfig,
    artifact_dir: Path | None,
) -> dict[str, Any]:
    variant: ExitRouteVariant = spec["variant"]
    status = str(job.get("status") if job else STATUS_QUEUED)
    result = job.get("result") if isinstance(job, dict) and isinstance(job.get("result"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    samples = _int(metrics.get("num_trades"))
    net = _float(metrics.get("net_profit_usd"))
    pf = _float(metrics.get("profit_factor"))
    win_rate = _float(metrics.get("win_rate_pct"))
    avg = _avg_net_bps(net, samples, config)
    source_avg = _float(spec.get("source_avg_net_bps"))
    uplift = None if avg is None or source_avg is None else round(avg - source_avg, 4)
    artifact_path = result.get("artifact_path")
    if not artifact_path and artifact_dir is not None and job is not None:
        candidate = artifact_dir / f"{job.get('job_id')}.json"
        artifact_path = str(candidate) if candidate.exists() else None
    verdict = _verdict(
        status,
        samples=samples,
        avg_net_bps=avg,
        profit_factor=pf,
        source_avg_net_bps=source_avg,
        maker_fill_modelled=variant.maker_fill_modelled,
        config=config,
    )
    return {
        "hypothesis_id": spec["hypothesis_id"],
        "job_id": job.get("job_id") if job else None,
        "status": status,
        "verdict": verdict,
        "variant_id": variant.variant_id,
        "variant_label": variant.label,
        "execution_route_model": variant.route_model,
        "route_assumption": variant.route_assumption,
        "maker_fill_modelled": variant.maker_fill_modelled,
        "source_action_id": spec["source_action_id"],
        "source_bucket": spec["source_bucket"],
        "source_avg_net_bps": source_avg,
        "source_profit_factor": _float(spec.get("source_profit_factor")),
        "source_samples": _int(spec.get("source_samples")),
        "required_uplift_bps": _float(spec.get("required_uplift_bps")),
        "port_id": spec["port_id"],
        "strategy_id": spec["strategy_id"],
        "adapter": spec["adapter"],
        "setup_mode": spec["setup_mode"],
        "canonical_adapter": bool(spec["canonical_adapter"]),
        "exchange": spec["exchange"],
        "symbol": spec["symbol"],
        "timeframe": spec["timeframe"],
        "max_holding_bars": spec["max_holding_bars"],
        "paper_margin_usd": config.paper_margin_usd,
        "paper_leverage": config.paper_leverage,
        "paper_notional_usd": config.paper_notional_usd,
        "samples": samples,
        "net_profit_usd": net,
        "avg_net_bps": avg,
        "uplift_bps": uplift,
        "profit_factor": pf,
        "win_rate_pct": win_rate,
        "blocked_reason": job.get("blocked_reason") if job else None,
        "error": job.get("error") if job else None,
        "artifact_path": artifact_path,
        "next_action": _next_action(verdict),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _avg_net_bps(
    net_profit_usd: float | None,
    samples: int,
    config: QuantifiedExitRouteUpliftConfig,
) -> float | None:
    if net_profit_usd is None or samples <= 0 or config.paper_notional_usd <= 0:
        return None
    return round(net_profit_usd / (config.paper_notional_usd * samples) * 10_000.0, 4)


def _verdict(
    status: str,
    *,
    samples: int,
    avg_net_bps: float | None,
    profit_factor: float | None,
    source_avg_net_bps: float | None,
    maker_fill_modelled: bool,
    config: QuantifiedExitRouteUpliftConfig,
) -> str:
    if status in {PENDING_STATUS, RUNNING_STATUS, STATUS_QUEUED}:
        return "AWAITING_UPLIFT_BACKTEST"
    if status == BLOCKED_STATUS:
        return "BLOCKED_DATA_OR_CONTRACT"
    if status == FAILED_STATUS:
        return "FAILED_REPLAY_ENGINE"
    if status != DONE_STATUS:
        return "UNKNOWN_STATUS"
    if samples <= 0:
        return "NO_TRADES"
    if avg_net_bps is None or profit_factor is None:
        return "INCOMPLETE_METRICS"
    clears = (
        avg_net_bps >= config.min_net_bps
        and profit_factor >= config.min_profit_factor
        and samples >= config.min_trades
    )
    if clears and maker_fill_modelled:
        return "ROUTE_MODEL_CLEARS_UPPER_BOUND_NEEDS_L2_FILL_PROOF"
    if clears:
        return "UPLIFT_CLEARS_EXPLORATORY_GATE_REQUIRES_UNTOUCHED_JUDGMENT"
    if source_avg_net_bps is not None and avg_net_bps > source_avg_net_bps:
        if avg_net_bps > 0:
            return "UPLIFT_IMPROVED_BUT_STILL_THIN"
        return "UPLIFT_LESS_NEGATIVE_ONLY"
    return "NO_UPLIFT"


def _next_action(verdict: str) -> str:
    if verdict == "UPLIFT_CLEARS_EXPLORATORY_GATE_REQUIRES_UNTOUCHED_JUDGMENT":
        return "QUEUE_UNTOUCHED_WINDOW_JUDGMENT"
    if verdict == "ROUTE_MODEL_CLEARS_UPPER_BOUND_NEEDS_L2_FILL_PROOF":
        return "RUN_L2_MAKER_FILL_PROOF_THEN_UNTOUCHED_JUDGMENT"
    if verdict == "UPLIFT_IMPROVED_BUT_STILL_THIN":
        return "MINE_CONTEXT_FILTERS_OR_REDUCE_ROUTE_ASSUMPTION"
    if verdict in {"AWAITING_UPLIFT_BACKTEST", "NO_TRADES"}:
        return "WAIT_FOR_AGENT_JOB_RUNNER"
    if verdict == "BLOCKED_DATA_OR_CONTRACT":
        return "REPAIR_DATA_COVERAGE_OR_SYMBOL_MAPPING"
    if verdict == "FAILED_REPLAY_ENGINE":
        return "REPAIR_REPLAY_ENGINE"
    return "REJECT_OR_KEEP_AS_CONTEXT_FEATURE"


def _summary(
    source_actions: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    created: int,
    config: QuantifiedExitRouteUpliftConfig,
) -> dict[str, Any]:
    statuses = Counter(str(row["status"]) for row in rows)
    verdicts = Counter(str(row["verdict"]) for row in rows)
    completed = [row for row in rows if row["status"] == DONE_STATUS]
    improved = [
        row
        for row in completed
        if row["uplift_bps"] is not None and float(row["uplift_bps"]) > 0.0
    ]
    clears = [
        row
        for row in completed
        if row["verdict"]
        in {
            "UPLIFT_CLEARS_EXPLORATORY_GATE_REQUIRES_UNTOUCHED_JUDGMENT",
            "ROUTE_MODEL_CLEARS_UPPER_BOUND_NEEDS_L2_FILL_PROOF",
        }
    ]
    best = max(
        (row for row in completed if row["uplift_bps"] is not None),
        key=lambda row: float(row["uplift_bps"]),
        default=None,
    )
    return {
        "source_actions": len(source_actions),
        "experiment_cells": len(rows),
        "jobs_created": created,
        "matched_jobs": sum(1 for row in rows if row.get("job_id")),
        "completed_cells": len(completed),
        "pending_cells": statuses.get(PENDING_STATUS, 0),
        "running_cells": statuses.get(RUNNING_STATUS, 0),
        "blocked_cells": statuses.get(BLOCKED_STATUS, 0),
        "failed_cells": statuses.get(FAILED_STATUS, 0),
        "queued_cells": statuses.get(STATUS_QUEUED, 0),
        "improved_cells": len(improved),
        "clears_exploratory_gate": len(clears),
        "route_upper_bound_cells": sum(
            1 for row in rows
            if row["verdict"] == "ROUTE_MODEL_CLEARS_UPPER_BOUND_NEEDS_L2_FILL_PROOF"
        ),
        "status_counts": dict(statuses),
        "verdict_counts": dict(verdicts),
        "best_uplift_bps": None if best is None else best["uplift_bps"],
        "best_avg_net_bps": None if best is None else best["avg_net_bps"],
        "best_lane": None if best is None else {
            "port_id": best["port_id"],
            "exchange": best["exchange"],
            "symbol": best["symbol"],
            "timeframe": best["timeframe"],
            "variant_id": best["variant_id"],
            "verdict": best["verdict"],
        },
        "gate": {
            "min_net_bps": config.min_net_bps,
            "min_profit_factor": config.min_profit_factor,
            "min_trades": config.min_trades,
        },
        "paper_profile": {
            "margin_usd": config.paper_margin_usd,
            "leverage": config.paper_leverage,
            "notional_usd": config.paper_notional_usd,
        },
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    if summary["clears_exploratory_gate"]:
        return (
            f"{summary['clears_exploratory_gate']} uplift experiment(s) clear the "
            "exploratory math gate. Next: untouched-window judgment; maker-route "
            "rows also need L2 fill proof."
        )
    if summary["improved_cells"]:
        return (
            f"{summary['improved_cells']} uplift experiment(s) improved the source "
            "cell but still need more edge before paper promotion."
        )
    if summary["jobs_created"]:
        return (
            f"Seeded {summary['jobs_created']} exit/route uplift backtest job(s); "
            "waiting for Agent Gateway runner evidence."
        )
    if summary["pending_cells"] or summary["running_cells"] or summary["queued_cells"]:
        return "Exit/route uplift jobs are queued or running; wait for completed evidence."
    if summary["source_actions"]:
        return "Near-fee-wall cells were found, but no uplift row has improved them yet."
    return "No Quantified near-fee-wall source actions are available for uplift."


def _policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "blocked_actions": [
            "promote_from_route_assumption",
            "paper_trade_from_title_inventory",
            "relax_fee_wall_gate",
        ],
        "promotion_boundary": (
            "An uplift row can only queue untouched-window judgment. Maker-first "
            "rows additionally require L2 fill proof before any promotion review."
        ),
    }


def _config_from_arbiter(
    arbiter_payload: Mapping[str, Any] | None,
    arbiter_path: Path | str | None,
) -> QuantifiedExitRouteUpliftConfig:
    arbiter = (
        dict(arbiter_payload)
        if arbiter_payload is not None
        else _read_json(Path(arbiter_path)) if arbiter_path is not None else {}
    )
    gate = {}
    summary = arbiter.get("summary") if isinstance(arbiter, Mapping) else {}
    if isinstance(summary, Mapping) and isinstance(summary.get("gate"), Mapping):
        gate = dict(summary["gate"])
    return QuantifiedExitRouteUpliftConfig(
        min_net_bps=_float(gate.get("min_net_bps")) or DEFAULT_GATE_NET_BPS,
        min_profit_factor=_float(gate.get("min_profit_factor")) or DEFAULT_GATE_PF,
        min_trades=_int(gate.get("min_trades")) or DEFAULT_GATE_TRADES,
    )


def _holding_bars(timeframe: str) -> int:
    return {
        "1m": 30,
        "5m": 18,
        "15m": 16,
        "1h": 12,
        "4h": 8,
    }.get(timeframe, QuantifiedBlueprintProofConfig().default_max_holding_bars)


def _feed_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ts": payload.get("generated_at"),
        "uplift_id": payload.get("uplift_id"),
        "summary": payload.get("summary"),
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish Quantified exit/route uplift queue")
    parser.add_argument("--arbiter", type=Path, default=DEFAULT_ARBITER)
    parser.add_argument("--jobs-dir", type=Path, default=env_agent_jobs_dir())
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--seed-jobs", action="store_true")
    parser.add_argument("--no-feed", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)

    while True:
        payload = build_quantified_exit_route_uplift_payload(
            arbiter_path=args.arbiter,
            jobs_dir=args.jobs_dir,
            artifact_dir=args.artifact_dir,
            seed_jobs=args.seed_jobs,
        )
        publish_quantified_exit_route_uplift(
            payload,
            out=args.out,
            feed=None if args.no_feed else args.feed,
        )
        summary = payload["summary"]
        print(
            "quantified exit-route uplift "
            f"{summary['completed_cells']}/{summary['experiment_cells']} completed / "
            f"{summary['jobs_created']} jobs created / "
            f"{summary['clears_exploratory_gate']} clears",
            flush=True,
        )
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
