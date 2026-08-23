"""Deterministic scanner replay and daily evidence artifacts.

This is an evidence tool, never a promotion or execution tool.  Replays use
the exact registered class and its frozen runtime holding contract.  Reports
separate gross movement from the configured round-trip cost estimate.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]

from vnedge.plan.cost_model import CostModel
from vnedge.strategy.scanner_contracts import scanner_runtime_contract
from vnedge.strategy.scanner_observability import enrich_evaluation
from vnedge.strategy.strategy_registry import get_strategy_class, is_capital_eligible


def replay_scanner(strategy_id: str, candles: pd.DataFrame) -> dict[str, Any]:
    """Replay one frozen scanner with the runtime's single-book semantics.

    ``signals`` remains the raw mechanism count. ``trades`` is the number the
    shadow lane could actually admit: an unresolved virtual position blocks a
    later signal through and including its exit bar. Entries use the first
    post-decision open, stop wins ties, and both realized execution cost and
    the conservative pre-trade gate cost are reported explicitly.
    """
    strategy = get_strategy_class(strategy_id)()
    prepared = strategy.prepare(candles)
    contract = scanner_runtime_contract(strategy_id)
    hold = contract.max_holding_bars if contract else 24
    cost_model = CostModel.for_profile(contract.cost_family if contract else "swing")
    execution_cost = float(cost_model.round_trip_bps(include_safety=False))
    gate_cost = float(cost_model.round_trip_bps(include_safety=True))
    position_active_through = -1
    admitted_trades = 0
    position_conflicts = 0
    rows: list[dict[str, Any]] = []
    for index in range(max(0, strategy.warmup_bars), len(prepared) - 1):
        intent = strategy.signal(prepared, index)
        explain = getattr(strategy, "evaluation_diagnostics", None)
        diagnostics = explain(prepared, index) if callable(explain) else {}
        base = {
            "bar_ts": str(prepared.iloc[index].get("timestamp", index)),
            "strategy_id": strategy_id,
            "fired": intent is not None,
            "eligible": bool(diagnostics.get("eligible", intent is not None)),
            "signal_reason": intent.reason if intent else None,
            "all_failed_gates": diagnostics.get("all_failed_gates", []),
            "features": diagnostics.get("features", {}),
            "thresholds": diagnostics.get("thresholds", {}),
            "distance_to_threshold": diagnostics.get("distance_to_threshold", {}),
        }
        record = enrich_evaluation(base)
        if intent is not None:
            if index <= position_active_through:
                position_conflicts += 1
                record["admitted"] = False
                record["runtime_block_reason"] = "shadow_book_unresolved"
                rows.append(record)
                continue
            entry_index = index + 1
            entry = float(prepared.iloc[entry_index]["open"])
            # Entry bar is holding age zero. Timeout occurs when age reaches
            # max_holding_bars, matching ShadowOutcomeTracker/backtester.
            timeout_index = entry_index + hold
            end = min(len(prepared) - 1, timeout_index)
            exit_price = float(prepared.iloc[end]["close"])
            resolution = "timeout" if end == timeout_index else "end_of_data"
            exit_index = end
            for cursor in range(entry_index, end + 1):
                bar = prepared.iloc[cursor]
                low, high = float(bar["low"]), float(bar["high"])
                bar_open = float(bar["open"])
                if intent.side == "long":
                    if low <= intent.stop_price:
                        exit_price = min(intent.stop_price, bar_open)
                        resolution = "stop"
                        exit_index = cursor
                        break
                    if intent.take_profit_price is not None and high >= intent.take_profit_price:
                        exit_price, resolution = intent.take_profit_price, "target"
                        exit_index = cursor
                        break
                else:
                    if high >= intent.stop_price:
                        exit_price = max(intent.stop_price, bar_open)
                        resolution = "stop"
                        exit_index = cursor
                        break
                    if intent.take_profit_price is not None and low <= intent.take_profit_price:
                        exit_price, resolution = intent.take_profit_price, "target"
                        exit_index = cursor
                        break
            sign = 1 if intent.side == "long" else -1
            gross = sign * (exit_price - entry) / entry * 10_000
            configured_cost = getattr(
                getattr(strategy, "params", None), "round_trip_cost_bps", None
            )
            realized_cost = float(configured_cost) if configured_cost is not None else execution_cost
            conservative_cost = max(realized_cost, gate_cost)
            record["admitted"] = True
            record["outcome"] = {
                "resolution": resolution,
                "entry": entry,
                "exit": exit_price,
                "gross_bps": gross,
                # Backward-compatible aliases retain the conservative number.
                "cost_bps": conservative_cost,
                "net_bps": gross - conservative_cost,
                "execution_cost_bps": realized_cost,
                "gate_cost_bps": conservative_cost,
                "net_execution_bps": gross - realized_cost,
                "net_gate_bps": gross - conservative_cost,
                "entry_index": entry_index,
                "exit_index": exit_index,
            }
            admitted_trades += 1
            position_active_through = exit_index
        rows.append(record)
    fired = [row for row in rows if row["fired"]]
    outcomes = [row["outcome"] for row in fired if row.get("admitted")]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "strategy_id": strategy_id,
        "capital_eligible": is_capital_eligible(strategy_id),
        "read_only": True,
        "bars": len(prepared),
        "evaluations": len(rows),
        "signals": len(fired),
        "trades": admitted_trades,
        "position_conflicts": position_conflicts,
        "gross_bps": sum(float(item["gross_bps"]) for item in outcomes),
        "net_execution_bps": sum(float(item["net_execution_bps"]) for item in outcomes),
        "net_gate_bps": sum(float(item["net_gate_bps"]) for item in outcomes),
        "net_bps": sum(float(item["net_gate_bps"]) for item in outcomes),
        "failed_gates": dict(Counter(
            gate for row in rows for gate in row.get("all_failed_gates", [])
        )),
        "lifecycle": dict(Counter(row["setup_lifecycle"] for row in rows)),
        "records": rows,
    }


def read_lane_evals(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("kind") != "lane_eval" or not isinstance(item.get("payload"), dict):
                    continue
                rows.append(enrich_evaluation(item["payload"]))
    return rows


def read_journal_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Read scanner evidence records with their durable lane provenance."""
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                rows.append(
                    {
                        "kind": str(item.get("kind") or ""),
                        "ts": item.get("ts"),
                        "lane_id": path.name.removesuffix(".journal.jsonl"),
                        "payload": payload,
                    }
                )
    return rows


def build_daily_report(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate evaluation truth by exact strategy id without ranking PnL."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("strategy_id") or "unknown")].append(row)
    strategies: list[dict[str, Any]] = []
    for strategy_id, items in sorted(grouped.items()):
        failures = Counter(
            str(gate) for item in items for gate in item.get("all_failed_gates", [])
        )
        lifecycle = Counter(str(item.get("setup_lifecycle") or "watching") for item in items)
        near: list[dict[str, Any]] = []
        for item in items:
            candidate = item.get("near_miss")
            if isinstance(candidate, dict):
                near.append(candidate)
        strategies.append({
            "strategy_id": strategy_id,
            "evaluations": len(items),
            "fires": sum(bool(item.get("fired")) for item in items),
            "backfill_evaluations": sum(bool(item.get("backfill")) for item in items),
            "failed_gates": dict(failures),
            "lifecycle": dict(lifecycle),
            "closest_near_miss": min(
                near,
                key=lambda item: float(item.get("closest_distance") or float("inf")),
                default=None,
            ),
            "capital_eligible": is_capital_eligible(strategy_id),
        })
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "evaluations": sum(item["evaluations"] for item in strategies),
        "fires": sum(item["fires"] for item in strategies),
        "strategies": strategies,
    }


def build_journal_report(paths: Iterable[Path]) -> dict[str, Any]:
    """Join evaluations, virtual intents and outcomes by durable intent key."""
    records = read_journal_records(paths)
    evaluations = [
        enrich_evaluation(record["payload"])
        for record in records
        if record["kind"] == "lane_eval"
    ]
    report = build_daily_report(evaluations)
    intents: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    for record in records:
        payload = record["payload"]
        key = str(payload.get("intent_key") or "")
        if not key:
            continue
        if record["kind"] == "shadow_intent" and key not in intents:
            intents[key] = {**record, "payload": dict(payload)}
        elif record["kind"] == "shadow_outcome" and key not in outcomes:
            outcomes[key] = {**record, "payload": dict(payload)}

    by_strategy = {row["strategy_id"]: row for row in report["strategies"]}
    for key, record in intents.items():
        payload = record["payload"]
        intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
        strategy_id = str(intent.get("strategy_id") or payload.get("strategy_id") or "unknown")
        row = by_strategy.setdefault(
            strategy_id,
            {
                "strategy_id": strategy_id,
                "evaluations": 0,
                "fires": 0,
                "backfill_evaluations": 0,
                "failed_gates": {},
                "lifecycle": {},
                "closest_near_miss": None,
                "capital_eligible": is_capital_eligible(strategy_id),
            },
        )
        row["virtual_candidates"] = int(row.get("virtual_candidates") or 0) + 1
        if bool(payload.get("approved")):
            row["virtual_approved"] = int(row.get("virtual_approved") or 0) + 1
        else:
            row["virtual_rejected"] = int(row.get("virtual_rejected") or 0) + 1
        outcome_record = outcomes.get(key)
        if outcome_record is None or not bool(payload.get("approved")):
            continue
        outcome = outcome_record["payload"]
        side = str(intent.get("side") or outcome.get("side") or "long")
        entry = float(outcome.get("entry_price") or 0.0)
        exit_price = float(outcome.get("exit_price") or 0.0)
        quantity = float(intent.get("quantity") or 0.0)
        direction = 1.0 if side == "long" else -1.0
        gross_usd = direction * quantity * (exit_price - entry)
        gross_bps = (
            direction * (exit_price - entry) / entry * 10_000.0
            if entry > 0 else 0.0
        )
        row["virtual_resolved"] = int(row.get("virtual_resolved") or 0) + 1
        row["gross_usd"] = float(row.get("gross_usd") or 0.0) + gross_usd
        row["gross_bps"] = float(row.get("gross_bps") or 0.0) + gross_bps
        row["fees_usd"] = float(row.get("fees_usd") or 0.0) + float(
            outcome.get("fees_usd") or 0.0
        )
        row["net_execution_usd"] = float(row.get("net_execution_usd") or 0.0) + float(
            outcome.get("virtual_net_usd") or 0.0
        )

    for strategy_id, row in by_strategy.items():
        approved = int(row.get("virtual_approved") or 0)
        resolved = int(row.get("virtual_resolved") or 0)
        row.setdefault("virtual_candidates", 0)
        row.setdefault("virtual_approved", 0)
        row.setdefault("virtual_rejected", 0)
        row.setdefault("virtual_resolved", 0)
        row["virtual_pending"] = max(0, approved - resolved)
        row.setdefault("gross_usd", 0.0)
        row.setdefault("gross_bps", 0.0)
        row.setdefault("fees_usd", 0.0)
        row.setdefault("net_execution_usd", 0.0)
        row["strategy_id"] = strategy_id
    report["schema_version"] = 2
    report["strategies"] = [by_strategy[key] for key in sorted(by_strategy)]
    report["virtual_candidates"] = sum(
        int(row["virtual_candidates"]) for row in report["strategies"]
    )
    report["virtual_resolved"] = sum(
        int(row["virtual_resolved"]) for row in report["strategies"]
    )
    report["net_execution_usd"] = sum(
        float(row["net_execution_usd"]) for row in report["strategies"]
    )
    return report


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, allow_nan=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", action="append", type=Path, default=[])
    parser.add_argument("--candles", type=Path)
    parser.add_argument("--strategy", action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.candles:
        if not args.strategy:
            parser.error("--candles requires at least one --strategy")
        candles = (
            pd.read_parquet(args.candles)
            if args.candles.suffix.lower() in {".parquet", ".pq"}
            else pd.read_csv(args.candles)
        )
        atomic_write(
            args.out,
            {
                "schema_version": 1,
                "generated_at": datetime.now(UTC).isoformat(),
                "read_only": True,
                "replays": [replay_scanner(strategy_id, candles) for strategy_id in args.strategy],
            },
        )
        return
    atomic_write(args.out, build_journal_report(args.journal))


if __name__ == "__main__":
    main()


__all__ = [
    "atomic_write", "build_daily_report", "build_journal_report",
    "read_journal_records", "read_lane_evals", "replay_scanner",
]
