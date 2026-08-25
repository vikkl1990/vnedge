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
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]

from vnedge.data.tape import clean_book
from vnedge.plan.cost_model import CostModel
from vnedge.runtime.latency_tracker import timeframe_to_seconds
from vnedge.runtime.scanner_engine import build_quote_acceptance_engine
from vnedge.strategy.scanner_contracts import (
    resolve_scanner_cost_profile,
    scanner_runtime_contract,
)
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


class _ReplayJournal:
    """Minimal in-memory journal implementing the runtime engine contract."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append({"kind": kind, "payload": payload})

    def read_all(self) -> list[dict[str, Any]]:
        return list(self.records)


def _event_time(row: pd.Series, *names: str) -> datetime:
    for name in names:
        if name not in row or pd.isna(row[name]):
            continue
        value = row[name]
        stamp = (
            pd.to_datetime(value, unit="ms", utc=True)
            if name.endswith("_ms")
            else pd.to_datetime(value, utc=True)
        )
        return stamp.to_pydatetime()
    raise ValueError(f"quote row has no event timestamp ({', '.join(names)})")


def replay_quote_scanner(
    strategy_id: str,
    candles: pd.DataFrame,
    quotes: pd.DataFrame,
    *,
    symbol: str = "BTC/USDT:USDT",
    exchange_id: str = "binanceusdm",
) -> dict[str, Any]:
    """Drive the runtime quote engine with recorded canonical bars and BBO.

    Event ordering matches the live loop: quotes strictly before a close see
    the previous arm; the closed bar is applied next; quotes exactly on the
    boundary see the new arm. Invalid BBO rows are removed through the same
    tape contract used by other raw-lake consumers and the dropped count is
    part of the evidence artifact.

    This is read-only evidence. The engine has no gateway callback and cannot
    submit an order.
    """
    contract = scanner_runtime_contract(strategy_id)
    if contract is None or not contract.decision_engine.startswith("quote_acceptance"):
        raise ValueError(f"{strategy_id} has no quote-acceptance runtime contract")
    strategy = get_strategy_class(strategy_id)()
    prepared = strategy.prepare(candles).reset_index(drop=True)
    if "timestamp" not in prepared.columns:
        raise ValueError("canonical candle frame requires timestamp")
    cleaned, cleaning = clean_book(quotes.copy())
    cleaned = cleaned.reset_index(drop=True)
    event_rows = sorted(
        (
            (_event_time(row, "ts_ms", "timestamp", "ts"), row)
            for _, row in cleaned.iterrows()
        ),
        key=lambda item: item[0],
    )
    journal = _ReplayJournal()
    tf_seconds = timeframe_to_seconds(contract.timeframe)
    if tf_seconds is None:
        raise ValueError(f"unsupported scanner timeframe {contract.timeframe}")
    engine = build_quote_acceptance_engine(
        journal=journal,
        symbol=symbol,
        strategy=strategy,
        contract=contract,
        cost_profile=resolve_scanner_cost_profile(contract, exchange_id=exchange_id),
        bar_minutes=tf_seconds / 60.0,
    )

    quote_index = 0

    def feed_quote(event_ts: datetime, row: pd.Series) -> None:
        received_ts = _event_time(row, "received_ts_ms", "received_ts") if (
            ("received_ts_ms" in row and not pd.isna(row["received_ts_ms"]))
            or ("received_ts" in row and not pd.isna(row["received_ts"]))
        ) else event_ts
        sequence_raw = row.get("sequence")
        sequence = None if pd.isna(sequence_raw) else sequence_raw
        engine.on_quote(
            bid=float(row["bid"]),
            ask=float(row["ask"]),
            ts=event_ts,
            received_ts=received_ts,
            sequence=sequence,
            source=str(row.get("source") or "recorded_book"),
            exchange_timestamped=bool(row.get("exchange_timestamped", True)),
            overflow_drops=int(row.get("overflow_drops") or 0),
        )

    candle_times = pd.to_datetime(prepared["timestamp"], utc=True)
    for bar_index, open_ts in enumerate(candle_times):
        close_ts = (open_ts + pd.Timedelta(seconds=tf_seconds)).to_pydatetime()
        while quote_index < len(event_rows) and event_rows[quote_index][0] < close_ts:
            feed_quote(*event_rows[quote_index])
            quote_index += 1
        engine.on_closed_bar(prepared, bar_index, close_ts)
        while quote_index < len(event_rows) and event_rows[quote_index][0] == close_ts:
            feed_quote(*event_rows[quote_index])
            quote_index += 1
    while quote_index < len(event_rows):
        feed_quote(*event_rows[quote_index])
        quote_index += 1

    intents = [r for r in journal.records if r["kind"] == "shadow_intent"]
    outcomes = [r for r in journal.records if r["kind"] == "shadow_outcome"]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "strategy_id": strategy_id,
        "symbol": symbol,
        "read_only": True,
        "data_source": "canonical_candles+clean_recorded_bbo",
        "bars": len(prepared),
        "quotes_in": cleaning.rows_in,
        "quotes_used": cleaning.rows_out,
        "quotes_dropped": cleaning.dropped,
        "intent_keys": [str(r["payload"].get("intent_key") or "") for r in intents],
        "intents": len(intents),
        "outcomes": len(outcomes),
        "engine": engine.stats(),
        "records": journal.records,
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


def _journal_lines(path: Path, *, max_bytes: int | None = None) -> Iterator[str]:
    """Yield complete UTF-8 JSONL records from a bounded file tail."""
    try:
        size = path.stat().st_size
        handle = path.open("rb")
    except OSError:
        return
    with handle:
        if max_bytes is not None and max_bytes > 0 and size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()  # discard the partial first record
        for raw in handle:
            yield raw.decode("utf-8", errors="replace")


def iter_journal_records(
    paths: Iterable[Path], *, max_bytes_per_journal: int | None = None
) -> Iterator[dict[str, Any]]:
    """Stream scanner records; never retain entire journals in memory."""
    for path in paths:
        for line in _journal_lines(path, max_bytes=max_bytes_per_journal):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            yield {
                "kind": str(item.get("kind") or ""),
                "ts": item.get("ts"),
                "lane_id": path.name.removesuffix(".journal.jsonl"),
                "payload": payload,
            }


def read_journal_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Compatibility materializer for callers/tests with already-bounded input."""
    return list(iter_journal_records(paths))


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


def build_journal_report(
    paths: Iterable[Path],
    *,
    max_bytes_per_journal: int = 8 * 1024 * 1024,
    max_total_bytes: int = 64 * 1024 * 1024,
) -> dict[str, Any]:
    """Join evaluations, virtual intents and outcomes by durable intent key."""
    journal_paths = tuple(paths)
    effective_bytes = min(
        max_bytes_per_journal,
        max(1, max_total_bytes // max(1, len(journal_paths))),
    )
    grouped: dict[str, dict[str, Any]] = {}
    pending_intents: dict[str, dict[str, Any]] = {}

    def strategy_row(strategy_id: str) -> dict[str, Any]:
        return grouped.setdefault(
            strategy_id,
            {
                "strategy_id": strategy_id,
                "evaluations": 0,
                "fires": 0,
                "backfill_evaluations": 0,
                "failed_gates": Counter(),
                "lifecycle": Counter(),
                "closest_near_miss": None,
                "capital_eligible": is_capital_eligible(strategy_id),
                "virtual_candidates": 0,
                "virtual_approved": 0,
                "virtual_rejected": 0,
                "virtual_resolved": 0,
                "gross_usd": 0.0,
                "gross_bps": 0.0,
                "fees_usd": 0.0,
                "net_execution_usd": 0.0,
            },
        )

    for record in iter_journal_records(
        journal_paths, max_bytes_per_journal=effective_bytes
    ):
        payload = record["payload"]
        if record["kind"] == "lane_eval":
            evaluation = enrich_evaluation(payload)
            strategy_id = str(evaluation.get("strategy_id") or "unknown")
            row = strategy_row(strategy_id)
            row["evaluations"] += 1
            row["fires"] += int(bool(evaluation.get("fired")))
            row["backfill_evaluations"] += int(bool(evaluation.get("backfill")))
            row["failed_gates"].update(
                str(gate) for gate in evaluation.get("all_failed_gates", [])
            )
            row["lifecycle"].update(
                (str(evaluation.get("setup_lifecycle") or "watching"),)
            )
            near = evaluation.get("near_miss")
            current = row["closest_near_miss"]
            if isinstance(near, dict) and (
                current is None
                or float(near.get("closest_distance") or float("inf"))
                < float(current.get("closest_distance") or float("inf"))
            ):
                row["closest_near_miss"] = near
        key = str(payload.get("intent_key") or "")
        if record["kind"] == "shadow_intent" and key:
            intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
            strategy_id = str(
                intent.get("strategy_id") or payload.get("strategy_id") or "unknown"
            )
            row = strategy_row(strategy_id)
            row["virtual_candidates"] += 1
            if bool(payload.get("approved")):
                row["virtual_approved"] += 1
                pending_intents.setdefault(
                    key,
                    {"strategy_id": strategy_id, "intent": dict(intent)},
                )
            else:
                row["virtual_rejected"] += 1
        elif record["kind"] == "shadow_outcome" and key:
            matched = pending_intents.pop(key, None)
            if matched is None:
                continue
            intent = matched["intent"]
            row = strategy_row(str(matched["strategy_id"]))
            side = str(intent.get("side") or payload.get("side") or "long")
            entry = float(payload.get("entry_price") or 0.0)
            exit_price = float(payload.get("exit_price") or 0.0)
            quantity = float(intent.get("quantity") or 0.0)
            direction = 1.0 if side == "long" else -1.0
            gross_usd = direction * quantity * (exit_price - entry)
            gross_bps = (
                direction * (exit_price - entry) / entry * 10_000.0
                if entry > 0
                else 0.0
            )
            row["virtual_resolved"] += 1
            row["gross_usd"] += gross_usd
            row["gross_bps"] += gross_bps
            row["fees_usd"] += float(payload.get("fees_usd") or 0.0)
            row["net_execution_usd"] += float(payload.get("virtual_net_usd") or 0.0)

    strategies = []
    for strategy_id in sorted(grouped):
        row = grouped[strategy_id]
        strategies.append(
            {
                **row,
                "failed_gates": dict(row["failed_gates"]),
                "lifecycle": dict(row["lifecycle"]),
            }
        )
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_window": {
            "mode": "bounded_journal_tail",
            "max_bytes_per_journal": max_bytes_per_journal,
            "effective_bytes_per_journal": effective_bytes,
            "max_total_bytes": max_total_bytes,
            "journals": len(journal_paths),
        },
        "read_only": True,
        "evaluations": sum(int(row["evaluations"]) for row in strategies),
        "fires": sum(int(row["fires"]) for row in strategies),
        "strategies": strategies,
    }

    by_strategy = {row["strategy_id"]: row for row in report["strategies"]}
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
    parser.add_argument(
        "--max-bytes-per-journal",
        type=int,
        default=8 * 1024 * 1024,
        help="bounded tail read per journal (default 8 MiB)",
    )
    parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=64 * 1024 * 1024,
        help="hard read budget across all journals (default 64 MiB)",
    )
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
    atomic_write(
        args.out,
        build_journal_report(
            args.journal,
            max_bytes_per_journal=args.max_bytes_per_journal,
            max_total_bytes=args.max_total_bytes,
        ),
    )


if __name__ == "__main__":
    main()


__all__ = [
    "atomic_write", "build_daily_report", "build_journal_report",
    "iter_journal_records", "read_journal_records", "read_lane_evals", "replay_scanner",
]
