"""Arena job adapter: preflight → freeze → existing evaluator → falsifier.

No model credentials, network clients, registry writes, or trading adapters.
Legacy generic research remains explicitly outside this governed entry point.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

import pandas as pd

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import PromotionGates
from vnedge.research.ai_candidate_research import (
    _ai_factory, _base_entry, _scan_ai_dir, ai_candidate_policy, evaluate_ai_candidate,
)
from vnedge.research.experiment_packet import (
    ExperimentSpec, digest, falsify, freeze_packet, json_bytes, persist_once,
    preflight, runtime_identity,
)
from vnedge.strategy.ai_sandbox import load_ai_strategy

MAX_CANDIDATES_PER_CYCLE = 8
MAX_INPUT_BARS = 20000


def run_governed_ai_research(
    store: Any, targets: Sequence[Any], *, strategy_dir: Path, out_dir: Path,
    candidate_offset: int = 0,
    previous_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    loaded, rejected = _scan_ai_dir(strategy_dir)
    candidates: list[dict[str, Any]] = []
    runtime = runtime_identity()
    allowed = {(t.exchange, t.symbol, t.timeframe) for t in targets}
    frames: dict[tuple[str, str, str], pd.DataFrame] = {}
    queue = sorted(loaded.items())
    offset = max(0, candidate_offset) % max(1, len(queue))
    queue = queue[offset:] + queue[:offset]
    previous = {c["strategy_id"]: c for c in (previous_candidates or [])
                if c.get("verdict") != "DEFERRED_BUDGET"}
    attempted = 0
    for strategy_id, (cls, filename) in queue:
        if strategy_id in previous:
            candidates.append(previous[strategy_id])
            continue
        entry = _base_entry(cls, filename)
        entry.update(verdict="NOT_TESTABLE", causality=None, walk_forward=None,
                     performance_eligible=False)
        if attempted >= MAX_CANDIDATES_PER_CYCLE:
            entry.update(verdict="DEFERRED_BUDGET", reasons=["cycle_candidate_budget"])
            candidates.append(entry)
            continue
        attempted += 1
        attempt_id = uuid4().hex
        source = (strategy_dir / filename).read_bytes()
        entry.update(attempt_id=attempt_id, source_sha256=digest(source))
        # Even a killed worker leaves an observable incomplete attempt.
        persist_once(out_dir / "attempts" / f"{attempt_id}.started.json", json_bytes({
            "attempt_id": attempt_id, "strategy_id": strategy_id,
            "source_sha256": digest(source), "started_at": now.isoformat(),
            "can_trade": False, "can_promote": False,
        }))
        try:
            contract_path = (strategy_dir / filename).with_suffix(".experiment.json")
            if not contract_path.is_file():
                raise ValueError("experiment_contract_missing")
            spec = ExperimentSpec.model_validate_json(
                contract_path.read_bytes()
            )
            # Execute the captured source, not a class from an earlier directory
            # scan if an external author changed the file between those reads.
            cls = load_ai_strategy(source.decode("utf-8"))
            target = (spec.exchange, spec.symbol, spec.timeframe)
            if target not in allowed:
                raise ValueError("declared_target_not_in_research_universe")
            if target not in frames:
                frame = store.read_candles(*target)
                if frame is None or frame.empty:
                    raise ValueError("canonical_history_missing")
                # Do not sort, fill, stamp, or silently discard questionable rows.
                # Select a bounded prefix ending at the store's last row.
                frames[target] = frame.tail(MAX_INPUT_BARS).copy().reset_index(drop=True)
            candles = frames[target]
            config = BacktestConfig(cost_profile=spec.cost_profile_id,
                                    max_holding_bars=spec.max_holding_bars)
            gates = PromotionGates()
            warmup = int(_ai_factory(cls)().warmup_bars)
            check = preflight(spec, candles, warmup_bars=warmup, now=now,
                              source_sha256=digest(source), strategy_id=cls.strategy_id)
            packet = freeze_packet(out_dir, spec, candles, source, config, asdict(gates), check, runtime)
            entry.update(packet_id=packet["packet_id"], preflight=check,
                         cost_profile_id=spec.cost_profile_id, entry_clock=spec.entry_clock,
                         booked_round_bps=packet["costs"]["booked_round_bps"],
                         dataset_sha256=packet["dataset_sha256"],
                         dataset={"exchange": spec.exchange, "symbol": spec.symbol,
                                  "timeframe": spec.timeframe, "bars": len(candles)})
            if check["status"] == "READY_TO_TEST":
                # Retain the existing engine and gates. No new fill simulator.
                result = evaluate_ai_candidate(
                    cls, filename, candles, None, grid=[{}], config=config, gates=gates,
                    train_bars=spec.train_bars, test_bars=spec.test_bars,
                    symbol=spec.symbol, timeframe=spec.timeframe, cut_points=None,
                )
                entry.update(result)
            else:
                entry["reasons"] = check["failures"]
            entry["falsification"] = falsify(packet, entry)
        except Exception as exc:  # isolate bad candidates; persist the explicit failure
            entry.update(verdict="NOT_TESTABLE" if isinstance(exc, (ValueError, FileNotFoundError)) else "ERROR",
                         reasons=[f"{type(exc).__name__}:{exc}"],
                         falsification={"kind": "deterministic_falsifier_v1",
                                        "status": "UNVERIFIED", "agreed": [], "contested": [],
                                        "unverified": ["experiment_incomplete"],
                                        "can_trade": False, "can_promote": False})
        entry.update(can_trade=False, can_promote=False, performance_eligible=False)
        persist_once(out_dir / "attempts" / f"{attempt_id}.result.json", json_bytes(entry))
        candidates.append(entry)
    return {
        "generated_at": now.isoformat(), "policy": ai_candidate_policy(),
        "dataset": {"source": "per_candidate_frozen_packet"},
        "candidates": candidates, "rejected_files": rejected,
        "governance": {"version": "experiment_packet_v1", "falsifier": "deterministic_not_llm",
                       "attempted_this_cycle": attempted,
                       "max_candidates_per_cycle": MAX_CANDIDATES_PER_CYCLE,
                       "max_input_bars": MAX_INPUT_BARS, "synthetic_fallback": False,
                       "next_candidate_offset": (offset + MAX_CANDIDATES_PER_CYCLE) % max(1, len(queue)),
                       "judgment_mode": "exploratory_not_untouched"},
        "can_trade": False, "can_promote": False,
    }
