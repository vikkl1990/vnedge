"""Explicit, immutable research intake. No automatic contracts for unknown code.

Copies a reviewed source into a NEW research ID; the legacy source and all old
results stay untouched. The supplied contract fixes market, clock and costs.
This CLI writes sandbox artifacts only, never the operational registry/roster.
"""
from __future__ import annotations

import argparse
import ast
import re
from datetime import UTC, datetime
from pathlib import Path

from vnedge.research.experiment_packet import ExperimentSpec, digest, json_bytes, persist_once
from vnedge.strategy.ai_sandbox import load_ai_strategy


def fork_candidate(source_path: Path, destination: Path, *, parent_sha256: str,
                   strategy_id: str, claim: str, exact_volume: bool = False) -> dict:
    if source_path.is_symlink() or destination.is_symlink():
        raise ValueError("symlink_refused")
    raw = source_path.read_bytes()
    if digest(raw) != parent_sha256:
        raise ValueError("reviewed_source_hash_mismatch")
    if not re.fullmatch(r"ai_[a-z0-9_]{4,140}", strategy_id):
        raise ValueError("invalid_research_id")
    text = raw.decode("utf-8")
    parent = load_ai_strategy(text)
    if strategy_id == parent.strategy_id:
        raise ValueError("new_strategy_id_required")
    tree = ast.parse(text)
    assignments = [n for c in tree.body if isinstance(c, ast.ClassDef) for n in c.body
                   if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "strategy_id"]
    if len(assignments) != 1 or assignments[0].lineno != assignments[0].end_lineno:
        raise ValueError("single_literal_strategy_id_required")
    assignment = assignments[0]
    if not isinstance(assignment.value, ast.Constant) or not isinstance(assignment.value.value, str):
        raise TypeError("literal_strategy_id_required")
    lines = text.splitlines(keepends=True)
    lines[assignment.lineno - 1] = " " * assignment.col_offset + f"strategy_id = {strategy_id!r}\n"
    new_raw = "".join(lines).encode()
    child = load_ai_strategy(new_raw.decode())
    if child.strategy_id != strategy_id:
        raise ValueError("child_identity_mismatch")
    spec = ExperimentSpec(
        strategy_id=strategy_id, source_sha256=digest(new_raw), claim=claim,
        invalidation="Reject if causal replay or frozen rolling OOS gates fail after declared costs; no institutional-flow claim.",
        exchange="delta_india", symbol="BTC/USD:USD", timeframe="1h",
        entry_clock="next_open", context_timeframes=(), exact_volume=exact_volume,
        cost_profile_id="delta_swing", train_bars=1440, test_bars=720,
        max_holding_bars=48, funding="excluded", judgment="exploratory_rolling_only",
    )
    target = destination / f"{strategy_id}.py"
    if target.resolve() == source_path.resolve():
        raise ValueError("parent_overwrite_refused")
    lineage = {"schema": "research_intake_v1", "parent_strategy_id": parent.strategy_id,
               "parent_sha256": parent_sha256, "strategy_id": strategy_id,
               "source_sha256": digest(new_raw), "contract_sha256": digest(json_bytes(spec.model_dump(mode="json"))),
               "scope": "new_exploratory_contract_not_retroactive_validation",
               "can_trade": False, "can_promote": False}
    # Contract and lineage precede discovery of the .py file. Interrupted writes
    # remain explicit conflicts, not a silently executable unbound candidate.
    persist_once(target.with_suffix(".experiment.json"), json_bytes(spec.model_dump(mode="json")))
    persist_once(target.with_suffix(".lineage.json"), json_bytes(lineage))
    persist_once(target, new_raw)
    return lineage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--parent-sha256", required=True)
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--claim", required=True)
    parser.add_argument("--exact-volume", action="store_true")
    parser.add_argument("--destination", type=Path, default=Path("data/strategies/ai"))
    args = parser.parse_args()
    result = fork_candidate(args.source, args.destination, parent_sha256=args.parent_sha256,
                            strategy_id=args.strategy_id, claim=args.claim, exact_volume=args.exact_volume)
    print(json_bytes({**result, "observed_at": datetime.now(UTC).isoformat()}).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
