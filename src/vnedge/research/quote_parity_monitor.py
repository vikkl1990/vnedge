"""Continuously prove lane-consumed quote replay parity for the active roster.

This service is deliberately read-only.  It loads the exact BBO rows captured
at each lane's ``on_quote`` boundary, reconstructs the same frozen scanner
engine, and compares its intents with that lane's durable journal.  A vacuous
zero-versus-zero comparison is reported as ``collecting`` rather than a pass.

Nothing in this module can enable the canonical router, mutate a strategy
registration, or grant execution authority.
"""

from __future__ import annotations

import argparse
import gc
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from vnedge.data.symbols import canonical_symbol
from vnedge.research.scanner_evidence import (
    atomic_write,
    compare_quote_replay_to_live,
    iter_journal_records,
    load_evidence_frame,
    normalize_canonical_candles,
    replay_quote_scanner,
)
from vnedge.runtime.multi_lane import LaneSpec
from vnedge.runtime.multi_lane_shadow import (
    OBSERVER_ROSTER_PATH_ENV,
    build_shadow_observe_roster_specs,
)
from vnedge.strategy.scanner_contracts import scanner_runtime_contract
from vnedge.strategy.strategy_registry import get_strategy_class


@dataclass(frozen=True, slots=True)
class QuoteParityPolicy:
    min_duration: timedelta = timedelta(hours=2)
    min_quotes: int = 1_000
    min_matched_intents: int = 1

    def __post_init__(self) -> None:
        if self.min_duration.total_seconds() <= 0:
            raise ValueError("minimum quote-parity duration must be positive")
        if self.min_quotes < 1 or self.min_matched_intents < 1:
            raise ValueError("quote and matched-intent minimums must be positive")


def classify_parity(
    replay: dict[str, Any],
    comparison: dict[str, Any],
    *,
    capture_duration: timedelta,
    policy: QuoteParityPolicy,
) -> tuple[str, tuple[str, ...]]:
    """Return a fail-closed operational verdict for one lane window."""

    reasons: list[str] = []
    quality = replay.get("capture_quality")
    if not isinstance(quality, dict) or not bool(quality.get("parity_eligible")):
        reasons.append("capture_not_parity_eligible")
        return "ineligible", tuple(reasons)
    if not bool(comparison.get("exact_parity")):
        reasons.append("live_replay_mismatch")
        return "mismatch", tuple(reasons)
    if capture_duration < policy.min_duration:
        reasons.append("capture_duration_below_minimum")
    if int(replay.get("quotes_used") or 0) < policy.min_quotes:
        reasons.append("quote_count_below_minimum")
    if int(comparison.get("matched_intents") or 0) < policy.min_matched_intents:
        reasons.append("matched_intents_below_minimum")
    return ("collecting", tuple(reasons)) if reasons else ("passed", ())


def latest_runtime_start(records: Iterable[dict[str, Any]], spec: LaneSpec) -> datetime | None:
    """Find the most recent runner-start heartbeat for exactly one lane."""

    starts: list[datetime] = []
    for record in records:
        if record.get("kind") != "paper_lane_heartbeat":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("reason") != "runner_started":
            continue
        if str(payload.get("strategy_id") or "") != spec.strategy_id:
            continue
        if str(payload.get("timeframe") or "") != spec.timeframe:
            continue
        try:
            payload_symbol = canonical_symbol(str(payload.get("symbol") or ""))
        except (TypeError, ValueError):
            continue
        if payload_symbol != spec.data_symbol:
            continue
        try:
            started = pd.to_datetime(payload.get("started_at"), utc=True).to_pydatetime()
        except (TypeError, ValueError):
            continue
        starts.append(started)
    return max(starts) if starts else None


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    value = record.get("ts")
    if value in (None, ""):
        return None
    try:
        return pd.to_datetime(value, utc=True).to_pydatetime()
    except (TypeError, ValueError):
        return None


def load_quote_capture(
    lane_root: Path,
    *,
    runtime_start: datetime,
    now: datetime,
) -> pd.DataFrame:
    """Load only shards whose UTC day can overlap the current runner."""

    if runtime_start.tzinfo is None or runtime_start.utcoffset() is None:
        raise ValueError("runtime_start must be timezone-aware")
    start_day = runtime_start.astimezone(UTC).strftime("%Y%m%d")
    end_day = now.astimezone(UTC).strftime("%Y%m%d")
    shards = [
        path
        for path in sorted(lane_root.rglob("*.parquet"))
        if start_day <= path.parent.name <= end_day
    ]
    if not shards:
        raise ValueError(f"no quote evidence for current runner under {lane_root}")
    frame = pd.concat((pd.read_parquet(path) for path in shards), ignore_index=True)
    required = {
        "ts_ms",
        "captured_at_ms",
        "capture_overflow_drops",
        "lane_id",
        "exchange",
        "symbol",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"quote evidence missing required columns: {missing}")
    event_time = pd.to_datetime(frame["ts_ms"], unit="ms", utc=True)
    keep = (event_time >= runtime_start) & (event_time <= now)
    frame = frame.loc[keep].copy()
    if frame.empty:
        raise ValueError("quote evidence has no rows inside current runner window")
    return frame.sort_values(["ts_ms", "captured_at_ms"], kind="stable").reset_index(drop=True)


def _candle_path(root: Path, spec: LaneSpec, timeframe: str) -> Path:
    return root / f"exchange={spec.exchange}" / spec.data_symbol / timeframe


def _context_frames(
    spec: LaneSpec,
    candle_root: Path,
    cache: dict[tuple[str, str, str], pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    strategy = get_strategy_class(spec.strategy_id)()
    contexts: dict[str, pd.DataFrame] = {}
    for timeframe in getattr(strategy, "canonical_context_timeframes", ()):
        key = (spec.exchange, spec.data_symbol, str(timeframe))
        if key not in cache:
            cache[key] = normalize_canonical_candles(
                load_evidence_frame(_candle_path(candle_root, spec, str(timeframe)))
            )
        contexts[str(timeframe)] = cache[key]
    return contexts


def _lane_report(
    spec: LaneSpec,
    *,
    candle_root: Path,
    quote_root: Path,
    journal_root: Path,
    policy: QuoteParityPolicy,
    max_journal_bytes: int,
    now: datetime,
    candle_cache: dict[tuple[str, str, str], pd.DataFrame],
) -> dict[str, Any]:
    contract = scanner_runtime_contract(spec.strategy_id)
    if contract is None or not contract.decision_engine.startswith("quote_acceptance"):
        return {
            "lane_id": spec.lane_id,
            "strategy_id": spec.strategy_id,
            "symbol": spec.data_symbol,
            "status": "not_applicable",
            "reasons": ["strategy_has_no_quote_acceptance_contract"],
        }
    journal_path = journal_root / f"{spec.lane_id}.journal.jsonl"
    if not journal_path.is_file():
        raise ValueError(f"lane journal unavailable: {journal_path}")
    records = list(iter_journal_records((journal_path,), max_bytes_per_journal=max_journal_bytes))
    runtime_start = latest_runtime_start(records, spec)
    if runtime_start is None:
        raise ValueError("current runner-start heartbeat is outside journal evidence")
    lane_capture = quote_root / f"lane={spec.lane_id}"
    quotes = load_quote_capture(lane_capture, runtime_start=runtime_start, now=now)
    lane_ids = {str(value) for value in quotes["lane_id"].dropna().unique()}
    exchanges = {str(value).strip().lower() for value in quotes["exchange"].dropna().unique()}
    symbols = {canonical_symbol(str(value)) for value in quotes["symbol"].dropna().unique()}
    if lane_ids != {spec.lane_id}:
        raise ValueError(f"quote evidence lane identity mismatch: {sorted(lane_ids)}")
    if exchanges != {spec.exchange}:
        raise ValueError(f"quote evidence exchange mismatch: {sorted(exchanges)}")
    if symbols != {spec.data_symbol}:
        raise ValueError(f"quote evidence symbol mismatch: {sorted(symbols)}")
    first_quote = pd.to_datetime(quotes["ts_ms"].iloc[0], unit="ms", utc=True).to_pydatetime()
    last_quote = pd.to_datetime(quotes["ts_ms"].iloc[-1], unit="ms", utc=True).to_pydatetime()
    record_times = [stamp for record in records if (stamp := _record_timestamp(record))]
    if not record_times or min(record_times) > first_quote:
        raise ValueError("journal tail does not cover the quote evidence window")
    candle_key = (spec.exchange, spec.data_symbol, spec.timeframe)
    if candle_key not in candle_cache:
        candle_cache[candle_key] = normalize_canonical_candles(
            load_evidence_frame(_candle_path(candle_root, spec, spec.timeframe))
        )
    replay = replay_quote_scanner(
        spec.strategy_id,
        candle_cache[candle_key],
        quotes,
        symbol=spec.symbol,
        exchange_id=spec.exchange,
        evidence_start=first_quote,
        evidence_end=last_quote + timedelta(milliseconds=1),
        runtime_start=runtime_start,
        context_candles=_context_frames(spec, candle_root, candle_cache),
    )
    comparison = compare_quote_replay_to_live(replay, records)
    duration = max(timedelta(0), last_quote - first_quote)
    status, reasons = classify_parity(replay, comparison, capture_duration=duration, policy=policy)
    replay_summary = {key: value for key, value in replay.items() if key != "records"}
    return {
        "lane_id": spec.lane_id,
        "strategy_id": spec.strategy_id,
        "exchange": spec.exchange,
        "symbol": spec.data_symbol,
        "timeframe": spec.timeframe,
        "runtime_start": runtime_start.isoformat(),
        "capture_start": first_quote.isoformat(),
        "capture_end": last_quote.isoformat(),
        "capture_duration_seconds": duration.total_seconds(),
        "status": status,
        "reasons": list(reasons),
        "replay": replay_summary,
        "comparison": comparison,
    }


def build_quote_parity_report(
    specs: Iterable[LaneSpec],
    *,
    candle_root: Path,
    quote_root: Path,
    journal_root: Path,
    policy: QuoteParityPolicy,
    max_journal_bytes: int = 128 * 1024 * 1024,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one immutable roster-wide cutover evidence snapshot."""

    generated = now or datetime.now(UTC)
    candle_cache: dict[tuple[str, str, str], pd.DataFrame] = {}
    rows: list[dict[str, Any]] = []
    for spec in specs:
        try:
            row = _lane_report(
                spec,
                candle_root=candle_root,
                quote_root=quote_root,
                journal_root=journal_root,
                policy=policy,
                max_journal_bytes=max_journal_bytes,
                now=generated,
                candle_cache=candle_cache,
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            row = {
                "lane_id": spec.lane_id,
                "strategy_id": spec.strategy_id,
                "exchange": spec.exchange,
                "symbol": spec.data_symbol,
                "timeframe": spec.timeframe,
                "status": "error",
                "reasons": [str(exc)],
            }
        rows.append(row)
        gc.collect()
    applicable = [row for row in rows if row["status"] != "not_applicable"]
    statuses = Counter(str(row["status"]) for row in rows)
    return {
        "schema_version": 1,
        "generated_at": generated.isoformat(),
        "read_only": True,
        "authority_changed": False,
        "router_decision_authority": False,
        "capital_enabled": False,
        "policy": {
            "min_duration_seconds": policy.min_duration.total_seconds(),
            "min_quotes": policy.min_quotes,
            "min_matched_intents": policy.min_matched_intents,
        },
        "summary": {
            "lanes": len(rows),
            "applicable_lanes": len(applicable),
            "statuses": dict(statuses),
            "cutover_ready": bool(applicable)
            and all(row["status"] == "passed" for row in applicable),
        },
        "lanes": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--candle-root", type=Path, default=Path("data/candles"))
    parser.add_argument("--quote-root", type=Path, default=Path("data/quote_evidence"))
    parser.add_argument("--journal-root", type=Path, default=Path("logs/paper_trials"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/live_research/quote_parity_status.json"),
    )
    parser.add_argument("--min-duration-minutes", type=float, default=120.0)
    parser.add_argument("--min-quotes", type=int, default=1_000)
    parser.add_argument("--min-matched-intents", type=int, default=1)
    parser.add_argument("--max-journal-bytes", type=int, default=128 * 1024 * 1024)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    args = parser.parse_args()
    policy = QuoteParityPolicy(
        min_duration=timedelta(minutes=args.min_duration_minutes),
        min_quotes=args.min_quotes,
        min_matched_intents=args.min_matched_intents,
    )
    specs = build_shadow_observe_roster_specs({OBSERVER_ROSTER_PATH_ENV: str(args.roster)})
    while True:
        atomic_write(
            args.output,
            build_quote_parity_report(
                specs,
                candle_root=args.candle_root,
                quote_root=args.quote_root,
                journal_root=args.journal_root,
                policy=policy,
                max_journal_bytes=args.max_journal_bytes,
            ),
        )
        if args.interval_seconds <= 0:
            return
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()


__all__ = [
    "QuoteParityPolicy",
    "build_quote_parity_report",
    "classify_parity",
    "latest_runtime_start",
    "load_quote_capture",
]
