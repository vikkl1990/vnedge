"""Bounded, continuous AI-strategy research pipeline.

This is an evidence factory, not an execution service.  It may materialize a
small, pre-registered catalogue of strategy *candidates* into the existing AI
sandbox, run the deny-by-default validator, prove truncation causality, execute
rolling walk-forward tests, and publish immutable evidence.  It cannot register
a strategy for execution, change a roster, promote a result, or place an order.

The proposal catalogue is deliberately finite and result-independent.  A bad
backtest cannot cause an open-ended parameter search: new source is selected in
catalogue order, at most one candidate per cycle.  An external AI may add source
to ``data/strategies/ai`` but that source enters exactly the same sandbox.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from vnedge.research.ai_candidate_research import (
    AI_CANDIDATES_LATEST,
    AI_STRATEGY_DIR,
    build_ai_candidates_payload,
    write_ai_candidates_payload,
)
from vnedge.strategy.ai_sandbox import validate_strategy_source
from vnedge.research.experiment_packet import ExperimentSpec, json_bytes, persist_once

PIPELINE_ID = "continuous_ai_research_v2"
DEFAULT_OUT_DIR = Path("research/live_research")
DEFAULT_LATEST = DEFAULT_OUT_DIR / "continuous_ai_pipeline_latest.json"
DEFAULT_FEED = DEFAULT_OUT_DIR / "continuous_ai_pipeline_feed.jsonl"
DEFAULT_EVIDENCE_DIR = DEFAULT_OUT_DIR / "ai_pipeline_evidence"
DEFAULT_ML_STATUS = DEFAULT_OUT_DIR / "ml_pipeline_status.json"


def _materialize_contract(destination: Path, blueprint: CandidateBlueprint, digest: str) -> None:
    spec = ExperimentSpec(
        strategy_id=f"ai_{blueprint.strategy_id}", source_sha256=digest,
        claim=blueprint.note,
        invalidation="Reject if frozen rolling OOS gates fail after declared taker costs.",
        cost_profile_id="delta_swing",
    )
    persist_once(destination.with_suffix(".experiment.json"), json_bytes(spec.model_dump(mode="json")))


@dataclass(frozen=True, slots=True)
class CandidateBlueprint:
    """One immutable, bounded mechanism proposal.

    Values are source-generation inputs, not optimizer axes.  Changing one is
    a new blueprint ID and therefore a new strategy ID/evidence stream.
    """

    blueprint_id: str
    class_name: str
    strategy_id: str
    fast_span: int
    slow_span: int
    atr_window: int
    stop_atr: float
    target_r: float
    note: str

    @property
    def filename(self) -> str:
        return f"auto_{self.strategy_id}.py"


BLUEPRINTS: tuple[CandidateBlueprint, ...] = (
    CandidateBlueprint(
        blueprint_id="ema_pullback_21_100_atr_v1",
        class_name="ArenaEmaPullback21100AI",
        strategy_id="arena_ema_pullback_21_100_v1",
        fast_span=21,
        slow_span=100,
        atr_window=20,
        stop_atr=1.5,
        target_r=2.0,
        note="closed-bar EMA trend pullback recovery; next-open research clock",
    ),
    CandidateBlueprint(
        blueprint_id="ema_pullback_50_200_atr_v1",
        class_name="ArenaEmaPullback50200AI",
        strategy_id="arena_ema_pullback_50_200_v1",
        fast_span=50,
        slow_span=200,
        atr_window=20,
        stop_atr=2.0,
        target_r=2.5,
        note="slower climate-aligned pullback recovery; next-open research clock",
    ),
    CandidateBlueprint(
        blueprint_id="ema_pullback_30_120_atr_v1",
        class_name="ArenaEmaPullback30120AI",
        strategy_id="arena_ema_pullback_30_120_v1",
        fast_span=30,
        slow_span=120,
        atr_window=30,
        stop_atr=1.75,
        target_r=3.0,
        note="medium-horizon trend resumption; next-open research clock",
    ),
)


def _source_for(blueprint: CandidateBlueprint) -> str:
    """Render one self-contained source file accepted by the AI sandbox."""

    return f'''"""Auto-created bounded Arena candidate.

Blueprint: {blueprint.blueprint_id}
Claim: {blueprint.note}
Research only.  This source is never auto-registered or executable by a venue.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr


class {blueprint.class_name}(BaseStrategy):
    strategy_id = "{blueprint.strategy_id}"
    warmup_bars = {blueprint.slow_span + 3}

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["fast"] = df["close"].ewm(span={blueprint.fast_span}, adjust=False, min_periods={blueprint.fast_span}).mean()
        df["slow"] = df["close"].ewm(span={blueprint.slow_span}, adjust=False, min_periods={blueprint.slow_span}).mean()
        df["atr"] = atr(df, {blueprint.atr_window})
        df["prior_close"] = df["close"].shift(1)
        df["prior_fast"] = df["fast"].shift(1)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars:
            return None
        row = df.iloc[index]
        required = (row["fast"], row["slow"], row["atr"], row["prior_close"], row["prior_fast"])
        if any(math.isnan(float(value)) for value in required):
            return None
        close = float(row["close"])
        fast = float(row["fast"])
        slow = float(row["slow"])
        bar_atr = float(row["atr"])
        prior_close = float(row["prior_close"])
        prior_fast = float(row["prior_fast"])
        if bar_atr <= 0:
            return None
        if fast > slow and prior_close <= prior_fast and close > fast:
            stop = close - {blueprint.stop_atr} * bar_atr
            risk = close - stop
            return SignalIntent(
                side="long",
                stop_price=stop,
                take_profit_price=close + {blueprint.target_r} * risk,
                reason="bounded EMA pullback recovery long",
            )
        if fast < slow and prior_close >= prior_fast and close < fast:
            stop = close + {blueprint.stop_atr} * bar_atr
            risk = stop - close
            return SignalIntent(
                side="short",
                stop_price=stop,
                take_profit_price=close - {blueprint.target_r} * risk,
                reason="bounded EMA pullback recovery short",
            )
        return None
'''


def _sha(value: bytes | str) -> str:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def materialize_next_blueprint(
    strategy_dir: Path | str,
    *,
    blueprints: Sequence[CandidateBlueprint] = BLUEPRINTS,
) -> dict[str, Any] | None:
    """Create at most one missing catalogued candidate, never overwrite.

    The rendered source is validated before it touches the candidate directory.
    Existing files are immutable: a filename collision with different content
    is reported as a conflict and never replaced.
    """

    root = Path(strategy_dir)
    root.mkdir(parents=True, exist_ok=True)
    for blueprint in blueprints:
        destination = root / blueprint.filename
        source = _source_for(blueprint)
        digest = _sha(source)
        if destination.exists():
            existing = _sha(destination.read_bytes())
            if existing != digest:
                return {
                    "status": "CONFLICT",
                    "blueprint_id": blueprint.blueprint_id,
                    "source_file": destination.name,
                    "expected_sha256": digest,
                    "actual_sha256": existing,
                    "reason": "candidate source is immutable; refusing overwrite",
                }
            try:
                _materialize_contract(destination, blueprint, digest)
            except ValueError as exc:
                return {"status": "CONFLICT", "source_file": destination.name, "reason": str(exc)}
            continue
        validation = validate_strategy_source(source)
        if not validation.ok:
            return {
                "status": "REFUSED_TEMPLATE",
                "blueprint_id": blueprint.blueprint_id,
                "source_file": destination.name,
                "reason": validation.describe(),
            }
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=root, prefix=f".{destination.name}.", delete=False
        ) as handle:
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, destination)
        _materialize_contract(destination, blueprint, digest)
        return {
            "status": "CREATED",
            "blueprint_id": blueprint.blueprint_id,
            "strategy_id": f"ai_{blueprint.strategy_id}",
            "source_file": destination.name,
            "source_sha256": digest,
            "params": asdict(blueprint),
            "can_trade": False,
            "can_promote": False,
        }
    return None


def _source_inventory(strategy_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not strategy_dir.exists():
        return rows
    for path in sorted(strategy_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        rows.append({"file": path.name, "sha256": _sha(path.read_bytes())})
        contract = path.with_suffix(".experiment.json")
        rows[-1]["contract_sha256"] = _sha(contract.read_bytes()) if contract.exists() else ""
    return rows


def _ordered_targets(targets: Sequence[Any]) -> tuple[Any, ...]:
    """Prefer the product venue and deepest symbol for the primary AI test.

    ``build_ai_candidates_payload`` selects the first BTC target. Reordering
    here keeps the generic research universe intact while ensuring this bot's
    Arena judges Delta India BTC before measurement-only venue cells.
    """

    return tuple(
        sorted(
            targets,
            key=lambda row: (
                str(getattr(row, "exchange", "")) != "delta_india",
                "BTC" not in str(getattr(row, "symbol", "")).upper(),
                str(getattr(row, "exchange", "")),
                str(getattr(row, "symbol", "")),
            ),
        )
    )


def _age_seconds(payload: dict[str, Any], now: datetime) -> float | None:
    raw = payload.get("last_evaluated_at") or payload.get("generated_at")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0.0, (now - stamp.astimezone(UTC)).total_seconds())


def _candidate_evidence(
    payload: dict[str, Any], inventory: list[dict[str, str]]
) -> list[dict[str, Any]]:
    source_hashes = {row["file"]: row["sha256"] for row in inventory}
    dataset = payload.get("dataset") if isinstance(payload.get("dataset"), dict) else {}
    rows: list[dict[str, Any]] = []
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        source_file = str(candidate.get("source_file") or "")
        identity = {
            "strategy_id": candidate.get("strategy_id"),
            "source_sha256": candidate.get("source_sha256") or source_hashes.get(source_file, ""),
            "dataset": dataset,
            "verdict": candidate.get("verdict"),
            "walk_forward": candidate.get("walk_forward"),
            "causality": candidate.get("causality"),
            "packet_id": candidate.get("packet_id"),
            "falsification": candidate.get("falsification"),
        }
        rows.append(
            {
                **candidate,
                "source_sha256": candidate.get("source_sha256") or source_hashes.get(source_file, ""),
                "evidence_id": _sha(json.dumps(identity, sort_keys=True, default=str))[:24],
                "can_trade": False,
                "can_promote": False,
            }
        )
    return rows


def run_continuous_ai_pipeline(
    store: Any,
    targets: Sequence[Any] = (),
    *,
    strategy_dir: Path | str = AI_STRATEGY_DIR,
    out_dir: Path | str = DEFAULT_OUT_DIR,
    auto_create: bool = False,
    force_evaluate: bool = False,
    retest_seconds: float = 86_400.0,
    queue_seconds: float = 3600.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one bounded create/test/evidence cycle.

    Expensive candidate evaluation is cached between daily research windows,
    but a newly created source forces immediate validation and backtesting.
    """

    generated_at = (now or datetime.now(UTC)).astimezone(UTC)
    root = Path(out_dir)
    strategy_root = Path(strategy_dir)
    latest_path = root / DEFAULT_LATEST.name
    previous = _read_json(latest_path)
    creation = materialize_next_blueprint(strategy_root) if auto_create else None
    created = bool(creation and creation.get("status") == "CREATED")
    inventory = _source_inventory(strategy_root)
    age = _age_seconds(previous, generated_at)
    ai_path = root / AI_CANDIDATES_LATEST
    cached_hash = _sha(ai_path.read_bytes()) if ai_path.is_file() else None
    refresh = (force_evaluate or created or inventory != previous.get("source_inventory")
           or previous.get("governance_version") != "canonical_queue_v2"
           or cached_hash is None or cached_hash != previous.get("evaluation_sha256")
           or age is None or age >= max(60.0, retest_seconds))
    pending = any(c.get("verdict") == "DEFERRED_BUDGET" for c in previous.get("candidates", []))
    last_batch = previous.get("last_batch_at") or previous.get("last_evaluated_at")
    batch_age = _age_seconds({"last_evaluated_at": last_batch}, generated_at)
    drain = pending and (batch_age is None or batch_age >= max(60.0, queue_seconds))
    due = refresh or drain

    if due:
        evaluation = build_ai_candidates_payload(
            store,
            _ordered_targets(targets),
            strategy_dir=strategy_root,
            experiment_dir=root / "experiments",
            candidate_offset=int((previous.get("governance") or {}).get("next_candidate_offset", 0)),
            previous_candidates=None if refresh else previous.get("candidates"),
        )
        write_ai_candidates_payload(evaluation, root)
        evaluation_status = "EVALUATED"
        last_evaluated_at = generated_at.isoformat() if refresh else str(previous.get("last_evaluated_at"))
    else:
        evaluation = _read_json(ai_path)
        evaluation_status = "CACHED"
        last_evaluated_at = str(previous.get("last_evaluated_at") or "")

    candidates = _candidate_evidence(evaluation, inventory)
    last_batch_at = generated_at.isoformat() if due else last_batch
    next_retest = datetime.fromisoformat(last_evaluated_at) + timedelta(seconds=max(60.0, retest_seconds))
    deferred = sum(c.get("verdict") == "DEFERRED_BUDGET" for c in candidates)
    next_queue = (datetime.fromisoformat(last_batch_at) + timedelta(seconds=max(60.0, queue_seconds))) if deferred else None
    next_due = min(next_retest, next_queue) if next_queue else next_retest
    verdicts: dict[str, int] = {}
    causal = 0
    for row in candidates:
        verdict = str(row.get("verdict") or "UNKNOWN")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if bool((row.get("causality") or {}).get("passed")):
            causal += 1

    ml = _read_json(root / DEFAULT_ML_STATUS.name)
    model_stage = str(ml.get("stage") or "UNAVAILABLE")
    model_samples = int((ml.get("dataset") or {}).get("samples") or 0)
    cycle_identity = {
        "pipeline_id": PIPELINE_ID,
        "generated_at": generated_at.isoformat(),
        "sources": inventory,
        "last_evaluated_at": last_evaluated_at,
        "candidate_evidence": [row.get("evidence_id") for row in candidates],
    }
    cycle_id = _sha(json.dumps(cycle_identity, sort_keys=True))[:24]
    status = "EVIDENCE_AVAILABLE" if candidates else "WAITING_FOR_PROPOSALS"
    lake_root = getattr(store, "root", None)
    lake_repair = _read_json(Path(lake_root).parent / "reports/delta_lake_repair.json") if lake_root is not None else {}
    recovery_plan = _read_json(Path(lake_root).parent / "reports/delta_recovery_plan.json") if lake_root is not None else {}
    if candidates and all(c.get("verdict") in {"NOT_TESTABLE", "ERROR", "DEFERRED_BUDGET"} for c in candidates):
        status = "BLOCKED_EVIDENCE"
    pipeline = {
        "pipeline_id": PIPELINE_ID,
        "cycle_id": cycle_id,
        "generated_at": generated_at.isoformat(),
        "last_evaluated_at": last_evaluated_at or None,
        "last_batch_at": last_batch_at,
        "next_evaluation_at": next_due.isoformat(),
        "next_retest_at": next_retest.isoformat(),
        "next_queue_at": next_queue.isoformat() if next_queue else None,
        "status": status,
        "lake_repair": lake_repair,
        "recovery_plan": recovery_plan,
        "governance_version": "canonical_queue_v2",
        "evaluation_sha256": _sha(ai_path.read_bytes()) if ai_path.is_file() else None,
        "source_inventory": inventory,
        "governance": evaluation.get("governance") or {},
        "evaluation_status": evaluation_status,
        "creation": creation,
        "stages": [
            {"key": "PROPOSE", "label": "Bounded proposals", "count": len(inventory), "state": "ACTIVE"},
            {"key": "PREFLIGHT", "label": "Data / cost preflight", "count": sum(c.get("preflight", {}).get("status") == "READY_TO_TEST" for c in candidates), "state": "REVIEW"},
            {"key": "PACKET", "label": "Frozen packets", "count": sum(bool(c.get("packet_id")) for c in candidates), "state": "RECORDED"},
            {"key": "SANDBOX", "label": "AST accepted", "count": len(candidates), "state": "PASS" if candidates else "WAIT"},
            {"key": "CAUSALITY", "label": "Causality", "count": causal, "state": "PASS" if causal else "WAIT"},
            {"key": "WALK_FORWARD", "label": "Rolling OOS", "count": sum(bool(c.get("walk_forward")) for c in candidates), "state": evaluation_status},
            {"key": "FALSIFY", "label": "Independent audit", "count": sum(bool(c.get("falsification")) for c in candidates), "state": "RESEARCH_ONLY"},
            {"key": "ML", "label": "Meta-label ML", "count": model_samples, "state": model_stage},
            {"key": "HUMAN_REVIEW", "label": "Human review", "count": verdicts.get("CANDIDATE", 0), "state": "LOCKED"},
        ],
        "summary": {
            "source_files": len(inventory),
            "discovered_candidates": len(candidates),
            "attempted_candidates": len(candidates) - deferred,
            "deferred_candidates": deferred,
            "attempted_this_cycle": int((evaluation.get("governance") or {}).get("attempted_this_cycle", 0)) if due else 0,
            "evaluated_candidates": sum(bool(c.get("causality")) for c in candidates),
            "backtested_candidates": sum(bool(c.get("walk_forward")) for c in candidates),
            "causal_candidates": causal,
            "candidate_verdicts": verdicts,
            "rejected_files": len(evaluation.get("rejected_files") or []),
            "untouched_judgment_queue": verdicts.get("CANDIDATE", 0),
            "model_stage": model_stage,
            "model_samples": model_samples,
        },
        "dataset": evaluation.get("dataset") or {},
        "candidates": candidates,
        "rejected_files": evaluation.get("rejected_files") or [],
        "ml": {
            "stage": model_stage,
            "samples": model_samples,
            "min_to_train": int((ml.get("dataset") or {}).get("min_to_train") or 200),
            "binding": False,
            "can_trade": False,
        },
        "policy": {
            "proposal_catalog": "finite_result_independent_v1",
            "max_new_sources_per_cycle": 1,
            "retest_seconds": max(60.0, retest_seconds),
            "queue_seconds": max(60.0, queue_seconds),
            "sandbox_required": True,
            "causality_required": True,
            "rolling_oos_required": True,
            "untouched_judgment_required": True,
            "human_approval_required": True,
            "auto_register": False,
            "roster_mutation": False,
            "capital_mutation": False,
            "can_trade": False,
            "can_promote": False,
        },
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
    }
    _atomic_json(latest_path, pipeline)
    if due or creation is not None:
        evidence_path = root / DEFAULT_EVIDENCE_DIR.name / f"{cycle_id}.json"
        _atomic_json(evidence_path, pipeline)
        _append_jsonl(
            root / DEFAULT_FEED.name,
            {
                "cycle_id": cycle_id,
                "generated_at": generated_at.isoformat(),
                "evidence_path": str(evidence_path),
                "evidence_sha256": _sha(evidence_path.read_bytes()),
                "summary": pipeline["summary"],
                "can_trade": False,
                "can_promote": False,
            },
        )
    return pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data/candles", help="Canonical recorder root; no OHLC fallback")
    parser.add_argument("--strategy-dir", default=AI_STRATEGY_DIR)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--interval-seconds", type=float, default=3600.0)
    parser.add_argument("--retest-seconds", type=float, default=86_400.0)
    parser.add_argument("--auto-create", action="store_true")
    parser.add_argument("--force-evaluate", action="store_true")
    args = parser.parse_args(argv)

    from vnedge.research.canonical_input import CanonicalResearchStore
    from vnedge.research.universe import load_research_targets

    store = CanonicalResearchStore(args.data_root)
    while True:
        result = run_continuous_ai_pipeline(
            store,
            load_research_targets(),
            strategy_dir=args.strategy_dir,
            out_dir=args.out_dir,
            auto_create=args.auto_create,
            force_evaluate=args.force_evaluate,
            retest_seconds=args.retest_seconds,
        )
        print(
            f"{PIPELINE_ID}: {result['evaluation_status']} "
            f"candidates={result['summary']['evaluated_candidates']} "
            f"queue={result['summary']['untouched_judgment_queue']}",
            flush=True,
        )
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
