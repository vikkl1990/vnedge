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

from vnedge.data.symbols import canonical_symbol
from vnedge.data.tape import TapeCleanResult, clean_book
from vnedge.exchange.book_imbalance import BookImbalance
from vnedge.plan.cost_model import CostModel
from vnedge.runtime.latency_tracker import timeframe_to_seconds
from vnedge.runtime.quote_ordering import quote_order_key
from vnedge.runtime.scanner_engine import build_quote_acceptance_engine
from vnedge.runtime.squeeze_observe import FireGuard
from vnedge.strategy.scanner_contracts import (
    resolve_scanner_cost_profile,
    scanner_runtime_contract,
)
from vnedge.strategy.scanner_observability import enrich_evaluation
from vnedge.strategy.strategy_registry import get_strategy_class, is_capital_eligible

#: Canonical lake columns stored as Decimal; strategies expect float math.
_CANONICAL_NUMERIC_COLUMNS = (
    "open", "high", "low", "close", "volume",
    "quote_volume", "trade_count", "taker_buy_volume", "vwap",
)


def load_evidence_frame(path: Path) -> pd.DataFrame:
    """Load one evidence input from a file or a shard directory.

    The recorder writes many small parquet shards per day (a single BBO day is
    thousands of files), and the canonical candle lake is one parquet file per
    day or month. A directory therefore concatenates every ``*.parquet`` under
    it, recursively, in sorted path order; a file loads as parquet or CSV by
    suffix. Sorting and dedup stay with the caller's normalizer because the
    time column differs per stream.
    """
    if path.is_dir():
        shards = sorted(path.rglob("*.parquet"))
        if not shards:
            raise ValueError(f"no parquet shards under directory {path}")
        return pd.concat(
            [pd.read_parquet(shard) for shard in shards], ignore_index=True
        )
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    return pd.read_csv(path)


def normalize_canonical_candles(candles: pd.DataFrame) -> pd.DataFrame:
    """Adapt canonical-lake candle frames to the runtime frame contract.

    The lake stores ``open_time`` (the runtime frame calls it ``timestamp``)
    and keeps prices as ``Decimal`` objects, which silently poison float
    feature math. Frames already in runtime form pass through unchanged, so
    every replay entrypoint can call this unconditionally.
    """
    frame = candles.copy()
    # CandleStore's Parquet schema contains closed candles only; ``read()``
    # reconstructs this same truth on the live path.  Preserve that contract
    # when the evidence input is the raw storage shape instead of making
    # exact-volume scanners fail readiness only in replay.
    storage_shape = "timestamp" not in frame.columns and "open_time" in frame.columns
    if storage_shape:
        frame = frame.rename(columns={"open_time": "timestamp"})
    if "timestamp" not in frame.columns:
        raise ValueError("canonical candle frame requires timestamp or open_time")
    for column in _CANONICAL_NUMERIC_COLUMNS:
        if column in frame.columns and frame[column].dtype == object:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    if storage_shape:
        if "is_closed" not in frame.columns:
            frame["is_closed"] = True
        if "data_quality" not in frame.columns:
            frame["data_quality"] = "ok"
        if "candle_source" not in frame.columns:
            frame["candle_source"] = "canonical_tick_lake"
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = (
        frame.sort_values("timestamp", kind="stable")
        .drop_duplicates(subset="timestamp", keep="last")
        .reset_index(drop=True)
    )
    return frame


def apply_contiguous_warmup_quality(
    candles: pd.DataFrame,
    *,
    timeframe_seconds: int,
    warmup_bars: int,
) -> tuple[pd.DataFrame, int]:
    """Propagate explicit canonical faults through the feature warm-up.

    Canonical storage deliberately skips empty buckets. Timestamp spacing is
    therefore not evidence of corruption and must not quarantine valid sparse
    tape. Only an existing non-OK state or an explicit timeout/repair marker
    starts a causal quarantine window.
    """
    if timeframe_seconds <= 0 or warmup_bars < 0:
        raise ValueError("timeframe_seconds must be positive and warmup_bars non-negative")
    frame = candles.copy()
    # Validate the boundary type, but never infer a fault from time spacing.
    pd.to_datetime(frame["timestamp"], utc=True)
    existing_ok = (
        frame["data_quality"].astype(str).str.lower().eq("ok")
        if "data_quality" in frame.columns
        else pd.Series(True, index=frame.index)
    )
    explicit_breaks = ~existing_ok
    for column in (
        "canonical_bar_timeout",
        "continuity_break",
        "missing_expected_child",
        "repair_hole",
    ):
        if column in frame.columns:
            explicit_breaks |= frame[column].fillna(False).astype(bool)
    break_count = int(explicit_breaks.sum())
    contaminated = (
        explicit_breaks.astype(int)
        .rolling(window=max(1, warmup_bars + 1), min_periods=1)
        .max()
        .astype(bool)
    )
    frame["data_quality"] = (~contaminated).map(
        {True: "ok", False: "gap"}
    )
    return frame, break_count


def _bind_replay_context(
    strategy: Any,
    context_candles: dict[str, pd.DataFrame] | None,
    *,
    contract_context: tuple[str, ...] | None = None,
) -> tuple[tuple[str, ...], dict[str, dict[str, int]]]:
    """Bind the same canonical HTF inputs required by the runtime strategy.

    A structure replay without its registered 4h context is not negative
    evidence; it is an incomplete engine.  Require every declared timeframe
    explicitly so a CLI mistake cannot silently become a zero-signal report.
    """
    required = tuple(
        str(value) for value in getattr(strategy, "canonical_context_timeframes", ())
    )
    if contract_context is not None and required != contract_context:
        raise ValueError(
            f"runtime context contract requires {contract_context}, "
            f"strategy declares {required}"
        )
    if not required:
        return (), {}
    binder = getattr(strategy, "bind_canonical_context", None)
    if not callable(binder):
        raise TypeError("strategy declares canonical context without a binder")
    supplied = context_candles or {}
    missing = [timeframe for timeframe in required if timeframe not in supplied]
    if missing:
        raise ValueError(
            "replay requires canonical context candle(s): " + ", ".join(missing)
        )
    diagnostics: dict[str, dict[str, int]] = {}
    for timeframe in required:
        frame = normalize_canonical_candles(supplied[timeframe])
        seconds = timeframe_to_seconds(timeframe)
        if seconds is None:
            raise ValueError(f"unsupported canonical context timeframe: {timeframe}")
        frame, breaks = apply_contiguous_warmup_quality(
            frame,
            timeframe_seconds=seconds,
            warmup_bars=0,
        )
        diagnostics[timeframe] = {
            "bars": len(frame),
            "continuity_breaks": breaks,
            "quarantined_bars": int(
                (~frame["data_quality"].astype(str).str.lower().eq("ok")).sum()
            ),
        }
        binder(timeframe, frame)
    return required, diagnostics


def replay_scanner(
    strategy_id: str,
    candles: pd.DataFrame,
    *,
    exchange_id: str = "binanceusdm",
    context_candles: dict[str, pd.DataFrame] | None = None,
    funding: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Replay one frozen scanner with the runtime's single-book semantics.

    ``signals`` remains the raw mechanism count. ``trades`` is the number the
    shadow lane could actually admit: an unresolved virtual position blocks a
    later signal through and including its exit bar. Entries use the first
    post-decision open, stop wins ties, and both realized execution cost and
    the conservative pre-trade gate cost are reported explicitly.
    """
    strategy = get_strategy_class(strategy_id)()
    contract = scanner_runtime_contract(strategy_id)
    bound_context, context_quality = _bind_replay_context(
        strategy,
        context_candles,
        contract_context=contract.context_tfs if contract is not None else None,
    )
    if contract is not None and contract.decision_engine.startswith("quote_acceptance"):
        raise ValueError(
            f"{strategy_id} is quote-driven ({contract.decision_engine}); "
            "closed-bar replay would fabricate its entry. Use "
            "replay_quote_scanner with lane-consumed BBO evidence."
        )
    normalized = normalize_canonical_candles(candles)
    if contract is not None:
        tf_seconds = timeframe_to_seconds(contract.timeframe)
    else:
        diffs = pd.to_datetime(normalized["timestamp"], utc=True).diff().dropna()
        tf_seconds = int(diffs.median().total_seconds()) if not diffs.empty else 1
    if tf_seconds is None:
        raise ValueError(f"unsupported scanner timeframe for {strategy_id}")
    normalized, continuity_breaks = apply_contiguous_warmup_quality(
        normalized,
        timeframe_seconds=tf_seconds,
        warmup_bars=int(strategy.warmup_bars),
    )
    prepared = strategy.prepare(normalized)
    hold = contract.max_holding_bars if contract else 24
    cost_profile = (
        resolve_scanner_cost_profile(contract, exchange_id=exchange_id)
        if contract is not None
        else ("delta_swing" if "delta" in exchange_id.lower() else "swing")
    )
    cost_model = CostModel.for_profile(cost_profile)
    execution_cost = float(cost_model.round_trip_bps(include_safety=False))
    gate_cost = float(cost_model.round_trip_bps(include_safety=True))
    entry_clock = (
        contract.evidence_entry_clock if contract is not None else "next_open"
    )
    clock_cohort = f"closed_{contract.timeframe if contract else 'bar'}->{entry_clock}"
    funding_frame = pd.DataFrame(columns=["timestamp", "funding_rate"])
    if funding is not None and not funding.empty:
        required_funding = {"timestamp", "funding_rate"}
        missing_funding = required_funding - set(funding.columns)
        if missing_funding:
            raise ValueError(
                "funding frame missing column(s): " + ", ".join(sorted(missing_funding))
            )
        funding_frame = funding.loc[:, ["timestamp", "funding_rate"]].copy()
        funding_frame["timestamp"] = pd.to_datetime(funding_frame["timestamp"], utc=True)
        funding_frame["funding_rate"] = pd.to_numeric(
            funding_frame["funding_rate"], errors="raise"
        )
        funding_frame = funding_frame.sort_values("timestamp", kind="stable")
    funding_included = not funding_frame.empty
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
            "entry_clock": entry_clock,
            "clock_cohort": clock_cohort,
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
            mfe_bps = 0.0
            mae_bps = 0.0
            for cursor in range(entry_index, end + 1):
                bar = prepared.iloc[cursor]
                low, high = float(bar["low"]), float(bar["high"])
                bar_open = float(bar["open"])
                if intent.side == "long":
                    mfe_bps = max(mfe_bps, (high - entry) / entry * 10_000)
                    mae_bps = min(mae_bps, (low - entry) / entry * 10_000)
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
                    mfe_bps = max(mfe_bps, (entry - low) / entry * 10_000)
                    mae_bps = min(mae_bps, (entry - high) / entry * 10_000)
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
            # Strategy parameters may contain a historical research estimate,
            # but execution evidence must use the runtime contract + venue.
            # A private strategy cost is never allowed to override GST,
            # slippage, or the safety reserve selected for this run.
            realized_cost = execution_cost
            conservative_cost = gate_cost
            entry_ts = pd.Timestamp(prepared.iloc[entry_index]["timestamp"])
            exit_ts = pd.Timestamp(prepared.iloc[exit_index]["timestamp"])
            funding_rates = funding_frame.loc[
                (funding_frame["timestamp"] > entry_ts)
                & (funding_frame["timestamp"] <= exit_ts),
                "funding_rate",
            ]
            # Perpetual convention: longs pay positive funding and shorts
            # receive it. Expressed in bps on entry notional for this
            # scanner-level evidence path.
            funding_bps = (
                (-1.0 if intent.side == "long" else 1.0)
                * float(funding_rates.sum())
                * 10_000.0
            )
            record["admitted"] = True
            record["outcome"] = {
                "resolution": resolution,
                "entry_clock": entry_clock,
                "clock_cohort": clock_cohort,
                "entry": entry,
                "exit": exit_price,
                "gross_bps": gross,
                # Unqualified cost/net fields mean booked execution economics.
                # The conservative pre-trade reserve remains explicitly gated.
                "cost_bps": realized_cost,
                "net_bps": gross - realized_cost + funding_bps,
                "execution_cost_bps": realized_cost,
                "gate_cost_bps": conservative_cost,
                "funding_bps": funding_bps,
                "net_execution_bps": gross - realized_cost + funding_bps,
                "net_gate_bps": gross - conservative_cost + funding_bps,
                "mae_bps": mae_bps,
                "mfe_bps": mfe_bps,
                "holding_bars": max(0, exit_index - entry_index),
                "hold_seconds": max(0.0, (exit_ts - entry_ts).total_seconds()),
                "entry_index": entry_index,
                "exit_index": exit_index,
            }
            admitted_trades += 1
            position_active_through = exit_index
        rows.append(record)
    fired = [row for row in rows if row["fired"]]
    outcomes = [row["outcome"] for row in fired if row.get("admitted")]
    blocked_by = Counter(
        gate for row in rows if not row["fired"] for gate in row.get("all_failed_gates", [])
    )
    closest_threshold: Counter[str] = Counter()
    for row in rows:
        near_miss = row.get("near_miss") or {}
        if not row["fired"] and near_miss.get("closest_metric"):
            closest_threshold[str(near_miss["closest_metric"])] += 1
    quarantined_bars = int(
        (~normalized["data_quality"].astype(str).str.lower().eq("ok")).sum()
    )
    evaluable_bars = max(0, len(prepared) - int(strategy.warmup_bars) - 1)
    return {
        "schema_version": 2,
        "net_bps_semantics": "booked_execution",
        "generated_at": datetime.now(UTC).isoformat(),
        "strategy_id": strategy_id,
        "entry_clock": entry_clock,
        "clock_cohort": clock_cohort,
        "capital_eligible": is_capital_eligible(strategy_id),
        "read_only": True,
        "exchange_id": exchange_id,
        "cost_profile": cost_profile,
        "execution_cost_bps": execution_cost,
        "gate_cost_bps": gate_cost,
        "funding_bps": sum(float(item["funding_bps"]) for item in outcomes),
        "funding_included": funding_included,
        "funding_event_count": len(funding_frame),
        "performance_eligible": False,
        "performance_blockers": (
            ([] if funding_included else ["funding_history_not_replayed"])
            + ["risk_gateway_not_replayed"]
        ),
        "bars": len(prepared),
        "canonical_context_timeframes": list(bound_context),
        "context_quality": context_quality,
        "continuity_breaks": continuity_breaks,
        "evaluations": len(rows),
        "signals": len(fired),
        "trades": admitted_trades,
        "position_conflicts": position_conflicts,
        "gross_bps": sum(float(item["gross_bps"]) for item in outcomes),
        "net_execution_bps": sum(float(item["net_execution_bps"]) for item in outcomes),
        "net_gate_bps": sum(float(item["net_gate_bps"]) for item in outcomes),
        "net_bps": sum(float(item["net_execution_bps"]) for item in outcomes),
        "failed_gates": dict(Counter(
            gate for row in rows for gate in row.get("all_failed_gates", [])
        )),
        "lifecycle": dict(Counter(row["setup_lifecycle"] for row in rows)),
        "setup_funnel": {
            "engine_status": (
                "ready" if evaluable_bars > 0 else "insufficient_warmup_window"
            ),
            "warmup_bars_required": int(strategy.warmup_bars),
            "evaluable_bars": evaluable_bars,
            "bars_quarantined": quarantined_bars,
            "evaluations": len(rows),
            "eligible_evaluations": sum(bool(row.get("eligible")) for row in rows),
            "signals": len(fired),
            "admitted_trades": admitted_trades,
            "near_misses": sum(
                not row["fired"] and bool(row.get("near_miss") or {}) for row in rows
            ),
            "blocked_by": dict(blocked_by),
            "closest_threshold": dict(closest_threshold),
        },
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
    evidence_start: datetime | None = None,
    evidence_end: datetime | None = None,
    runtime_start: datetime | None = None,
    context_candles: dict[str, pd.DataFrame] | None = None,
    approve_fire: FireGuard | None = None,
    approval_parity_eligible: bool | None = None,
    approval_mode: str | None = None,
) -> dict[str, Any]:
    """Drive the runtime quote engine with recorded canonical bars and BBO.

    Event ordering matches the live loop: quotes strictly before a close see
    the previous arm; the closed bar is applied next; quotes exactly on the
    boundary see the new arm. Lane-consumed evidence is not cleaned again:
    replay must feed the exact rows the live engine consumed. External book
    evidence is cleaned for diagnostics but remains parity-ineligible.

    This is read-only evidence and cannot submit an order. Approval parity is
    eligible only when the caller supplies the same deterministic ``FireGuard``
    as the lane. A caller may supply a recorded lifecycle guard while explicitly
    keeping ``approval_parity_eligible=False``: that lets rejected live
    candidates re-arm the replay engine without pretending the shared purse,
    candle-health latch, funding snapshot, or risk gateway were reconstructed.
    """
    symbol = canonical_symbol(symbol)
    contract = scanner_runtime_contract(strategy_id)
    if contract is None or not contract.decision_engine.startswith("quote_acceptance"):
        raise ValueError(f"{strategy_id} has no quote-acceptance runtime contract")
    strategy = get_strategy_class(strategy_id)()
    bound_context, context_quality = _bind_replay_context(
        strategy,
        context_candles,
        contract_context=contract.context_tfs,
    )
    tf_seconds = timeframe_to_seconds(contract.timeframe)
    if tf_seconds is None:
        raise ValueError(f"unsupported scanner timeframe {contract.timeframe}")
    normalized = normalize_canonical_candles(candles)
    normalized, continuity_breaks = apply_contiguous_warmup_quality(
        normalized,
        timeframe_seconds=tf_seconds,
        warmup_bars=int(strategy.warmup_bars),
    )
    if runtime_start is not None:
        if runtime_start.tzinfo is None or runtime_start.utcoffset() is None:
            raise ValueError("runtime_start must be timezone-aware")
        times = pd.to_datetime(normalized["timestamp"], utc=True)
        closed_before_start = times + pd.Timedelta(seconds=tf_seconds) <= runtime_start
        seed_indices = normalized.index[closed_before_start]
        if seed_indices.empty:
            raise ValueError("runtime_start has no causal warm-up candles")
        last_seed = int(seed_indices[-1])
        first_seed = max(0, last_seed - int(strategy.warmup_bars))
        normalized = normalized.iloc[first_seed:].reset_index(drop=True)
    prepared = strategy.prepare(normalized).reset_index(drop=True)
    if prepared.empty:
        raise ValueError("canonical candle frame is empty")
    if "timestamp" not in prepared.columns:
        raise ValueError("canonical candle frame requires timestamp")
    capture_overflow_drops = (
        int(
            pd.to_numeric(
                quotes["capture_overflow_drops"], errors="coerce"
            ).fillna(0).max()
        )
        if "capture_overflow_drops" in quotes.columns and not quotes.empty
        else 0
    )
    if capture_overflow_drops > 0:
        raise ValueError(
            "quote evidence capture overflowed; parity window is incomplete"
        )
    lane_consumed_capture = {
        "lane_id",
        "captured_at_ms",
        "capture_overflow_drops",
    }.issubset(quotes.columns)
    if lane_consumed_capture:
        cleaned = quotes.copy()
        cleaning = TapeCleanResult(rows_in=len(quotes), rows_out=len(quotes))
    else:
        cleaned, cleaning = clean_book(quotes.copy())
    cleaned = cleaned.reset_index(drop=True)
    candle_times = pd.to_datetime(prepared["timestamp"], utc=True)
    window_start = candle_times.iloc[0].to_pydatetime()
    # Quotes after the final closed bar remain causal during the immediately
    # forming bar. At its close another candle event is required, so evidence
    # beyond that boundary must not run against a stale arm.
    window_end = (
        candle_times.iloc[-1] + pd.Timedelta(seconds=tf_seconds * 2)
    ).to_pydatetime()
    ordered_rows: list[tuple[datetime, datetime, Any, int, pd.Series]] = []
    for ordinal, (_, row) in enumerate(cleaned.iterrows()):
        event_ts = _event_time(row, "ts_ms", "timestamp", "ts")
        received_ts = (
            _event_time(row, "received_ts_ms", "received_ts")
            if (
                ("received_ts_ms" in row and not pd.isna(row["received_ts_ms"]))
                or ("received_ts" in row and not pd.isna(row["received_ts"]))
            )
            else event_ts
        )
        sequence_raw = row.get("sequence")
        sequence = None if pd.isna(sequence_raw) else sequence_raw
        ordered_rows.append((event_ts, received_ts, sequence, ordinal, row))
    ordered_rows.sort(
        key=lambda item: quote_order_key(item[0], item[1], item[2], item[3])
    )
    all_event_rows = [(item[0], item[4]) for item in ordered_rows]
    replay_event_start = max(window_start, runtime_start) if runtime_start else window_start
    event_rows = [
        item for item in all_event_rows if replay_event_start <= item[0] < window_end
    ]
    quotes_outside_window = len(all_event_rows) - len(event_rows)
    # The audited evidence window is where BOTH sides could have acted: replay
    # needs warm-up candles from before BBO coverage begins, but comparing that
    # span against live journals would count every pre-coverage live intent as
    # a replay miss. Default start is therefore the first clean recorded quote;
    # explicit bounds are clamped inside the causal source window.
    first_quote_ts = event_rows[0][0] if event_rows else window_start
    if evidence_start is None:
        audit_start = first_quote_ts
        audit_basis = "first_clean_quote"
    else:
        audit_start = max(replay_event_start, evidence_start)
        audit_basis = "explicit"
    audit_end = window_end if evidence_end is None else min(window_end, evidence_end)
    journal = _ReplayJournal()
    cost_profile = resolve_scanner_cost_profile(contract, exchange_id=exchange_id)
    entry_clock = contract.evidence_entry_clock
    clock_cohort = f"closed_{contract.timeframe}->{entry_clock}"
    engine = build_quote_acceptance_engine(
        journal=journal,
        symbol=symbol,
        strategy=strategy,
        contract=contract,
        cost_profile=cost_profile,
        bar_minutes=tf_seconds / 60.0,
        approve_fire=approve_fire,
        require_book_imbalance=exchange_id.lower() in {
            "delta",
            "delta_india",
            "deltaindia",
        },
    )

    quote_index = 0

    def feed_quote(event_ts: datetime, row: pd.Series) -> None:
        received_ts = _event_time(row, "received_ts_ms", "received_ts") if (
            ("received_ts_ms" in row and not pd.isna(row["received_ts_ms"]))
            or ("received_ts" in row and not pd.isna(row["received_ts"]))
        ) else event_ts
        sequence_raw = row.get("sequence")
        sequence = None if pd.isna(sequence_raw) else sequence_raw
        book: BookImbalance | None = None
        book_columns = {
            "book_ts_ms",
            "bid_size",
            "ask_size",
            "book_imbalance",
            "microprice",
            "spread_ticks",
            "book_levels",
        }
        if book_columns.issubset(row.index) and not any(
            pd.isna(row[name]) for name in book_columns
        ):
            book = BookImbalance(
                bid=float(row["bid"]),
                ask=float(row["ask"]),
                bid_size=float(row["bid_size"]),
                ask_size=float(row["ask_size"]),
                imb=float(row["book_imbalance"]),
                microprice=float(row["microprice"]),
                spread_ticks=float(row["spread_ticks"]),
                ts=datetime.fromtimestamp(float(row["book_ts_ms"]) / 1000.0, tz=UTC),
                levels=int(row["book_levels"]),
            )
        engine.on_quote(
            bid=float(row["bid"]),
            ask=float(row["ask"]),
            ts=event_ts,
            received_ts=received_ts,
            sequence=sequence,
            source=str(row.get("source") or "recorded_book"),
            exchange_timestamped=bool(row.get("exchange_timestamped", True)),
            overflow_drops=int(row.get("overflow_drops") or 0),
            book=book,
        )

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

    approval_eligible = (
        approve_fire is not None
        if approval_parity_eligible is None
        else bool(approval_parity_eligible)
    )
    resolved_approval_mode = approval_mode or (
        "shared_fire_guard" if approval_eligible else "mechanism_only"
    )
    intents = [r for r in journal.records if r["kind"] == "shadow_intent"]
    outcomes = [r for r in journal.records if r["kind"] == "shadow_outcome"]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "strategy_id": strategy_id,
        "entry_clock": entry_clock,
        "clock_cohort": clock_cohort,
        "cost_profile": cost_profile,
        "symbol": symbol,
        "read_only": True,
        "evidence_class": "quote_mechanism_parity",
        "data_source": (
            "canonical_candles+lane_consumed_bbo"
            if lane_consumed_capture
            else "canonical_candles+clean_external_bbo"
        ),
        "source_window": {
            "start": window_start.isoformat(),
            "end_exclusive": window_end.isoformat(),
        },
        "evidence_window": {
            "start": audit_start.isoformat(),
            "end_exclusive": audit_end.isoformat(),
            "basis": audit_basis,
        },
        "runtime_start": runtime_start.isoformat() if runtime_start is not None else None,
        "bars": len(prepared),
        "canonical_context_timeframes": list(bound_context),
        "context_quality": context_quality,
        "continuity_breaks": continuity_breaks,
        "quotes_in": cleaning.rows_in,
        "quotes_clean": cleaning.rows_out,
        "quotes_used": len(event_rows),
        "quotes_dropped": cleaning.dropped,
        "quotes_outside_window": quotes_outside_window,
        "capture_quality": {
            "mode": "lane_consumed" if lane_consumed_capture else "external_book",
            "queue_overflow_drops": capture_overflow_drops,
            "complete": capture_overflow_drops == 0,
            # A standalone book recorder owns another websocket connection.
            # It is useful diagnostic evidence, but its event sequence cannot
            # prove exact parity with the quotes consumed by the live lane.
            "parity_eligible": (
                lane_consumed_capture and capture_overflow_drops == 0
            ),
        },
        "approval_parity_eligible": approval_eligible,
        "approval_mode": resolved_approval_mode,
        # Historical funding is absent, so even a shared approval guard cannot
        # make this a promotion-grade PnL backtest.
        "performance_eligible": False,
        "performance_blockers": (
            (
                []
                if approval_eligible
                else [
                    "risk_gateway_not_replayed",
                    "candle_health_not_replayed",
                    "shared_portfolio_not_replayed",
                ]
            )
            + ["funding_history_not_replayed"]
        ),
        "intent_keys": [str(r["payload"].get("intent_key") or "") for r in intents],
        "intents": len(intents),
        "outcomes": len(outcomes),
        "engine": engine.stats(),
        "records": journal.records,
    }


def _record_event_time(record: dict[str, Any]) -> datetime | None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    for value in (
        payload.get("quote_event_ts"),
        payload.get("bar_ts"),
        record.get("ts"),
    ):
        if value in (None, ""):
            continue
        try:
            return pd.to_datetime(value, utc=True).to_pydatetime()
        except (TypeError, ValueError):
            continue
    return None


def _intent_signature(
    payload: dict[str, Any], *, include_approval: bool
) -> dict[str, Any]:
    intent_value = payload.get("intent")
    intent: dict[str, Any] = intent_value if isinstance(intent_value, dict) else {}
    key_parts = str(payload.get("intent_key") or "").split("|")
    signature = {
        "side": str(
            intent.get("side")
            or payload.get("side")
            or (key_parts[2] if len(key_parts) >= 3 else "")
        ),
        "entry_price": payload.get("entry_price"),
        "stop_price": payload.get("stop_price"),
        # Parquet may materialize a native integer sequence as a string while
        # the live JSON journal retains it as an int.  That is one venue
        # identity, not a payload mismatch.  The ordering path already uses
        # the same numeric-first normalization; parity must do likewise.
        "quote_sequence": _normalized_quote_sequence(
            payload.get("quote_sequence")
        ),
        "episode_id": payload.get("episode_id"),
    }
    if include_approval:
        normalized_intent = dict(intent)
        if normalized_intent.get("symbol"):
            try:
                normalized_intent["symbol"] = canonical_symbol(
                    str(normalized_intent["symbol"])
                )
            except (TypeError, ValueError):
                pass
        signature.update(
            {
                "approved": bool(payload.get("approved")),
                "failed_checks": tuple(payload.get("failed_checks") or ()),
                "passed_checks": tuple(payload.get("passed_checks") or ()),
                "intent": normalized_intent,
            }
        )
    return signature


def _normalized_quote_sequence(value: object) -> int | str | None:
    """Canonicalize a venue sequence without conflating distinct values."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else str(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _canonical_intent_key(key: str) -> str:
    """Normalize the symbol segment without hiding temporal/lifecycle drift."""
    parts = key.split("|")
    if len(parts) < 4:
        return key
    try:
        parts[1] = canonical_symbol(parts[1])
    except (TypeError, ValueError):
        return key
    return "|".join(parts)


def compare_quote_replay_to_live(
    replay: dict[str, Any],
    live_records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Compare exact quote-scanner intents inside the replay source window.

    The result is evidence only. It cannot mutate registry state or grant
    promotion. Duplicate keys, missing keys, additional live keys, and
    decision-payload mismatches all fail parity explicitly.
    """
    strategy_id = str(replay.get("strategy_id") or "")
    symbol = canonical_symbol(str(replay.get("symbol") or ""))
    source_window = replay.get("source_window")
    if not isinstance(source_window, dict):
        raise TypeError("quote replay is missing source_window")
    # Audit inside the evidence window when the replay declares one: the
    # source window includes warm-up history from before BBO coverage, where
    # only the live side could have produced intents.
    audit_window = replay.get("evidence_window")
    if not isinstance(audit_window, dict):
        audit_window = source_window
    start = pd.to_datetime(audit_window.get("start"), utc=True).to_pydatetime()
    end = pd.to_datetime(audit_window.get("end_exclusive"), utc=True).to_pydatetime()

    def relevant(records: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        selected: list[dict[str, Any]] = []
        malformed = 0
        for record in records:
            if record.get("kind") != "shadow_intent":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            intent_value = payload.get("intent")
            intent: dict[str, Any] = (
                intent_value if isinstance(intent_value, dict) else {}
            )
            key = str(payload.get("intent_key") or "")
            parts = key.split("|")
            record_strategy = str(
                intent.get("strategy_id")
                or payload.get("strategy_id")
                or (parts[0] if len(parts) >= 2 else "")
            )
            record_symbol = str(
                intent.get("symbol")
                or payload.get("symbol")
                or (parts[1] if len(parts) >= 2 else "")
            )
            try:
                identity_matches = (
                    record_strategy == strategy_id
                    and bool(record_symbol)
                    and canonical_symbol(record_symbol) == symbol
                )
            except (TypeError, ValueError):
                identity_matches = False
            if not identity_matches:
                if record_strategy == strategy_id or not record_strategy:
                    malformed += 1
                continue
            event_ts = _record_event_time(record)
            if event_ts is None or not (start <= event_ts < end):
                continue
            selected.append(record)
        return selected, malformed

    replay_records, malformed_replay = relevant(replay.get("records") or [])
    live_selected, malformed_live = relevant(live_records)

    approval_eligible = bool(replay.get("approval_parity_eligible", False))

    def keyed(
        records: list[dict[str, Any]],
    ) -> tuple[Counter[str], dict[str, list[dict[str, Any]]]]:
        counts: Counter[str] = Counter()
        signatures: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            payload = record["payload"]
            key = _canonical_intent_key(str(payload.get("intent_key") or ""))
            if not key:
                continue
            counts[key] += 1
            signatures[key].append(
                _intent_signature(payload, include_approval=approval_eligible)
            )
        return counts, dict(signatures)

    replay_counts, replay_signatures = keyed(replay_records)
    live_counts, live_signatures = keyed(live_selected)
    replay_only = list((replay_counts - live_counts).elements())
    live_only = list((live_counts - replay_counts).elements())
    matched_keys = sorted(replay_counts.keys() & live_counts.keys())
    payload_mismatches = [
        {
            "intent_key": key,
            "replay": replay_signatures[key],
            "live": live_signatures[key],
        }
        for key in matched_keys
        if replay_signatures[key] != live_signatures[key]
    ]
    duplicate_replay = {key: count for key, count in replay_counts.items() if count > 1}
    duplicate_live = {key: count for key, count in live_counts.items() if count > 1}
    capture_quality = replay.get("capture_quality")
    input_eligible = True
    input_ineligible_reasons: list[str] = []
    if isinstance(capture_quality, dict):
        input_eligible = bool(capture_quality.get("parity_eligible", False))
        if not input_eligible:
            mode = str(capture_quality.get("mode") or "unknown")
            input_ineligible_reasons.append(f"quote_capture_not_parity_eligible:{mode}")
    mechanism_exact = input_eligible and not any(
        (
            replay_only,
            live_only,
            payload_mismatches,
            duplicate_replay,
            duplicate_live,
            malformed_replay,
            malformed_live,
        )
    )
    exact = mechanism_exact and approval_eligible
    if not approval_eligible:
        input_ineligible_reasons.append("approval_fire_guard_not_replayed")
    matched = sum((replay_counts & live_counts).values())
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "source_window": source_window,
        "evidence_window": audit_window,
        "capture_quality": capture_quality,
        "input_eligible": input_eligible,
        "input_ineligible_reasons": input_ineligible_reasons,
        "mechanism_exact_parity": mechanism_exact,
        "approval_parity_eligible": approval_eligible,
        "exact_parity": exact,
        "replay_intents": sum(replay_counts.values()),
        "live_intents": sum(live_counts.values()),
        "matched_intents": matched,
        "replay_only": replay_only,
        "live_only": live_only,
        "duplicate_replay": duplicate_replay,
        "duplicate_live": duplicate_live,
        "malformed_replay_records": malformed_replay,
        "malformed_live_records": malformed_live,
        "payload_mismatches": payload_mismatches,
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
            "accepted_entries": None,
            "quote_lifecycle_complete": False,
        })
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "evidence_scope": "closed_bar_evaluations_only",
        "accepted_entries": None,
        "quote_lifecycle_complete": False,
        "evaluations": sum(item["evaluations"] for item in strategies),
        "fires": sum(item["fires"] for item in strategies),
        "strategies": strategies,
    }


def build_journal_report(
    paths: Iterable[Path],
    *,
    max_bytes_per_journal: int | None = None,
    max_total_bytes: int | None = None,
) -> dict[str, Any]:
    """Build a lifecycle report from unique durable keys and transitions.

    Full streaming reads are the correctness default. Callers may request a
    bounded tail, but that artifact is explicitly incomplete and never treated
    as performance evidence.
    """
    journal_paths = tuple(paths)
    positive_per = (
        max_bytes_per_journal
        if max_bytes_per_journal is not None and max_bytes_per_journal > 0
        else None
    )
    positive_total = (
        max_total_bytes if max_total_bytes is not None and max_total_bytes > 0 else None
    )
    if positive_per is not None and positive_total is not None:
        effective_bytes: int | None = min(
            positive_per, max(1, positive_total // max(1, len(journal_paths)))
        )
    else:
        effective_bytes = positive_per
    grouped: dict[str, dict[str, Any]] = {}
    intents: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    intent_duplicates: Counter[str] = Counter()
    outcome_duplicates: Counter[str] = Counter()

    def payload_strategy(payload: dict[str, Any], key: str = "") -> str:
        intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
        parts = key.split("|")
        return str(
            intent.get("strategy_id")
            or payload.get("strategy_id")
            or (parts[0] if len(parts) >= 2 else "unknown")
        )

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
                "quote_lifecycle": Counter(),
                "scanner_transitions": 0,
                "armed_episodes": set(),
                "closest_near_miss": None,
                "capital_eligible": is_capital_eligible(strategy_id),
                "virtual_candidates": 0,
                "virtual_approved": 0,
                "virtual_rejected": 0,
                "virtual_resolved": 0,
                "gross_usd": 0.0,
                "gross_bps": 0.0,
                "fees_usd": 0.0,
                "funding_usd": 0.0,
                "net_execution_usd": 0.0,
                "observed_shadow_net_usd": 0.0,
                "unmatched_outcomes": 0,
                "duplicate_intents": 0,
                "duplicate_outcomes": 0,
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
        if record["kind"] == "scanner_transition":
            strategy_id = payload_strategy(payload)
            row = strategy_row(strategy_id)
            state = str(payload.get("state") or "unknown")
            row["quote_lifecycle"].update((state,))
            row["scanner_transitions"] += 1
            episode = payload.get("episode_id")
            if state.startswith("armed_") and episode is not None:
                row["armed_episodes"].add(
                    (str(payload.get("symbol") or ""), str(episode))
                )
        key = str(payload.get("intent_key") or "")
        if record["kind"] == "shadow_intent" and key:
            if key in intents:
                intent_duplicates[key] += 1
            # Latest durable verdict wins; retries no longer create phantom
            # candidates and a later approval is not hidden by setdefault.
            intents[key] = {
                **payload,
                "_lane_id": record["lane_id"],
                "_record_ts": record["ts"],
            }
        elif record["kind"] == "shadow_outcome" and key:
            if key in outcomes:
                outcome_duplicates[key] += 1
            outcomes[key] = {
                **payload,
                "_lane_id": record["lane_id"],
                "_record_ts": record["ts"],
            }

    approved_keys: set[str] = set()
    for key, payload in intents.items():
        strategy_id = payload_strategy(payload, key)
        row = strategy_row(strategy_id)
        row["virtual_candidates"] += 1
        row["duplicate_intents"] += intent_duplicates[key]
        if bool(payload.get("approved")):
            row["virtual_approved"] += 1
            approved_keys.add(key)
        else:
            row["virtual_rejected"] += 1

    resolved_approved: set[str] = set()
    resolved_trades: list[dict[str, Any]] = []
    for key, payload in outcomes.items():
        matched = intents.get(key)
        strategy_id = (
            payload_strategy(payload, key)
            if payload.get("strategy_id")
            else payload_strategy(matched, key)
            if matched is not None
            else payload_strategy(payload, key)
        )
        row = strategy_row(strategy_id)
        row["virtual_resolved"] += 1
        row["duplicate_outcomes"] += outcome_duplicates[key]
        if matched is None:
            row["unmatched_outcomes"] += 1
        elif bool(matched.get("approved")):
            resolved_approved.add(key)

        intent = (
            matched.get("intent")
            if matched is not None and isinstance(matched.get("intent"), dict)
            else {}
        )
        side = str(intent.get("side") or payload.get("side") or "long")
        entry = float(payload.get("entry_price") or 0.0)
        exit_price = float(payload.get("exit_price") or 0.0)
        quantity = float(intent.get("quantity") or 0.0)
        direction = 1.0 if side == "long" else -1.0
        computed_gross_usd = direction * quantity * (exit_price - entry)
        computed_gross_bps = (
            direction * (exit_price - entry) / entry * 10_000.0
            if entry > 0
            else 0.0
        )
        row["gross_usd"] += float(
            payload.get("gross_pnl_usd")
            if payload.get("gross_pnl_usd") is not None
            else computed_gross_usd
        )
        row["gross_bps"] += float(
            payload.get("captured_bps")
            if payload.get("captured_bps") is not None
            else computed_gross_bps
        )
        row["fees_usd"] += float(payload.get("fees_usd") or 0.0)
        row["funding_usd"] += float(payload.get("funding_usd") or 0.0)
        booked_net_usd = float(payload.get("virtual_net_usd") or 0.0)
        row["net_execution_usd"] += booked_net_usd
        # Compatibility name for the dashboard. It remains explicitly
        # shadow-only and is never converted into a replay ``net_bps``.
        row["observed_shadow_net_usd"] += booked_net_usd
        resolved_trades.append(
            {
                "lane": str(payload.get("_lane_id") or (matched or {}).get("_lane_id") or ""),
                "ts": str(payload.get("exit_ts") or payload.get("_record_ts") or ""),
                "kind": "shadow_outcome",
                "strategy_id": strategy_id,
                "symbol": str(payload.get("symbol") or intent.get("symbol") or ""),
                "side": side,
                "resolution": str(payload.get("resolution") or "resolved"),
                "entry_price": entry,
                "exit_price": exit_price,
                "quantity": quantity,
                "notional_usd": float(intent.get("notional_usd") or 0.0),
                "leverage": float(intent.get("leverage") or 0.0),
                "virtual_net_usd": booked_net_usd,
                "gross_pnl_usd": float(
                    payload.get("gross_pnl_usd")
                    if payload.get("gross_pnl_usd") is not None
                    else computed_gross_usd
                ),
                "captured_bps": float(
                    payload.get("captured_bps")
                    if payload.get("captured_bps") is not None
                    else computed_gross_bps
                ),
                "fees_usd": float(payload.get("fees_usd") or 0.0),
                "funding_usd": float(payload.get("funding_usd") or 0.0),
                "net_bps": payload.get("net_bps"),
                "mfe_bps": payload.get("mfe_bps"),
                "mae_bps": payload.get("mae_bps"),
                "entry_ts": str(payload.get("entry_ts") or (matched or {}).get("_record_ts") or ""),
                "exit_ts": str(payload.get("exit_ts") or payload.get("_record_ts") or ""),
                "bars_held": int(payload.get("bars_held") or 0),
                "cost_profile": str(payload.get("cost_profile") or "legacy_unattributed"),
                "cost_contract_version": str(payload.get("cost_contract_version") or "legacy"),
                "build_sha": str(payload.get("build_sha") or "unknown"),
                "intent_key": key,
                "signal_reason": str(
                    payload.get("signal_reason")
                    or (matched or {}).get("signal_reason")
                    or ""
                ),
                "take_profit_levels": payload.get("take_profit_levels") or [],
                "tp_reached": int(payload.get("tp_reached") or 0),
                "source": "scanner_evidence_full_stream",
            }
        )

    strategies = []
    for strategy_id in sorted(grouped):
        row = grouped[strategy_id]
        strategies.append(
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key != "armed_episodes"
                },
                "failed_gates": dict(row["failed_gates"]),
                "lifecycle": dict(row["lifecycle"]),
                "quote_lifecycle": dict(row["quote_lifecycle"]),
                "armed_entries": len(row["armed_episodes"]),
            }
        )
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_window": {
            "mode": "full_journal_stream" if effective_bytes is None else "bounded_journal_tail",
            "complete": effective_bytes is None,
            "max_bytes_per_journal": max_bytes_per_journal,
            "effective_bytes_per_journal": effective_bytes,
            "max_total_bytes": max_total_bytes,
            "journals": len(journal_paths),
        },
        "read_only": True,
        "performance_eligible": False,
        "performance_blockers": [
            "shadow_observation_not_backtest",
            *([] if effective_bytes is None else ["bounded_journal_tail"]),
        ],
        "evaluations": sum(int(row["evaluations"]) for row in strategies),
        "fires": sum(int(row["fires"]) for row in strategies),
        "strategies": strategies,
    }

    by_strategy = {row["strategy_id"]: row for row in report["strategies"]}
    for strategy_id, row in by_strategy.items():
        approved = int(row.get("virtual_approved") or 0)
        row.setdefault("virtual_candidates", 0)
        row.setdefault("virtual_approved", 0)
        row.setdefault("virtual_rejected", 0)
        row.setdefault("virtual_resolved", 0)
        row["virtual_pending"] = sum(
            key in approved_keys
            and key not in resolved_approved
            and payload_strategy(payload, key) == strategy_id
            for key, payload in intents.items()
        )
        # ``fires`` is deliberately the closed-bar decision count.  Quote
        # scanners arm on a closed bar and fire only after live acceptance,
        # so expose the actual accepted-entry count separately instead of
        # presenting a working quote scanner as ``0 fires`` in the UI.
        row["accepted_entries"] = approved
        row.setdefault("gross_usd", 0.0)
        row.setdefault("gross_bps", 0.0)
        row.setdefault("fees_usd", 0.0)
        row.setdefault("net_execution_usd", 0.0)
        row.setdefault("observed_shadow_net_usd", 0.0)
        row["strategy_id"] = strategy_id
    # Schema 3 adds the compact resolved-trade ledger used by bounded UI
    # readers. Schema 2 aggregate fields retain their meanings unchanged.
    report["schema_version"] = 3
    report["strategies"] = [by_strategy[key] for key in sorted(by_strategy)]
    report["virtual_candidates"] = sum(
        int(row["virtual_candidates"]) for row in report["strategies"]
    )
    report["virtual_resolved"] = sum(
        int(row["virtual_resolved"]) for row in report["strategies"]
    )
    report["virtual_approved"] = sum(
        int(row["virtual_approved"]) for row in report["strategies"]
    )
    report["virtual_rejected"] = sum(
        int(row["virtual_rejected"]) for row in report["strategies"]
    )
    report["virtual_pending"] = sum(
        int(row["virtual_pending"]) for row in report["strategies"]
    )
    report["accepted_entries"] = sum(
        int(row["accepted_entries"]) for row in report["strategies"]
    )
    report["scanner_transitions"] = sum(
        int(row["scanner_transitions"]) for row in report["strategies"]
    )
    lifecycle: Counter[str] = Counter()
    quote_lifecycle: Counter[str] = Counter()
    for row in report["strategies"]:
        lifecycle.update(dict(row.get("lifecycle") or {}))
        quote_lifecycle.update(dict(row.get("quote_lifecycle") or {}))
    report["lifecycle"] = dict(lifecycle)
    report["quote_lifecycle"] = dict(quote_lifecycle)
    report["gross_usd"] = sum(
        float(row["gross_usd"]) for row in report["strategies"]
    )
    report["gross_bps"] = sum(
        float(row["gross_bps"]) for row in report["strategies"]
    )
    report["fees_usd"] = sum(
        float(row["fees_usd"]) for row in report["strategies"]
    )
    report["funding_usd"] = sum(
        float(row["funding_usd"]) for row in report["strategies"]
    )
    report["net_execution_usd"] = sum(
        float(row["net_execution_usd"]) for row in report["strategies"]
    )
    report["observed_shadow_net_usd"] = sum(
        float(row["observed_shadow_net_usd"]) for row in report["strategies"]
    )
    report["armed_entries"] = sum(
        int(row["armed_entries"]) for row in report["strategies"]
    )
    report["unmatched_outcomes"] = sum(
        int(row["unmatched_outcomes"]) for row in report["strategies"]
    )
    report["resolved_trades"] = sorted(
        resolved_trades,
        key=lambda item: (str(item.get("ts") or ""), str(item.get("intent_key") or "")),
        reverse=True,
    )
    report["resolved_trades_complete"] = effective_bytes is None
    if report["unmatched_outcomes"]:
        report["performance_blockers"].append("unmatched_outcome_lifecycle")
    return report


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, allow_nan=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _load_context_arguments(values: list[str]) -> dict[str, pd.DataFrame]:
    contexts: dict[str, pd.DataFrame] = {}
    for value in values:
        timeframe, separator, raw_path = value.partition("=")
        if not separator or not timeframe.strip() or not raw_path.strip():
            raise ValueError("--context-candles must be TIMEFRAME=PATH")
        key = timeframe.strip()
        if key in contexts:
            raise ValueError(f"duplicate canonical context timeframe: {key}")
        contexts[key] = normalize_canonical_candles(
            load_evidence_frame(Path(raw_path.strip()))
        )
    return contexts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", action="append", type=Path, default=[])
    parser.add_argument("--candles", type=Path)
    parser.add_argument(
        "--context-candles",
        action="append",
        default=[],
        metavar="TIMEFRAME=PATH",
        help="canonical HTF frame required by a scanner; repeat per timeframe",
    )
    parser.add_argument("--quotes", type=Path)
    parser.add_argument("--symbol", default="BTC/USDT:USDT")
    parser.add_argument("--exchange-id", default="binanceusdm")
    parser.add_argument("--strategy", action="append", default=[])
    parser.add_argument(
        "--evidence-start",
        help="ISO start of the audited parity window (default: first clean recorded quote)",
    )
    parser.add_argument(
        "--evidence-end",
        help="ISO exclusive end of the audited parity window (default: end of causal source window)",
    )
    parser.add_argument(
        "--runtime-start",
        help="ISO start of the uninterrupted live runner instance being replayed",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--max-bytes-per-journal",
        type=int,
        default=0,
        help="optional bounded tail per journal; 0 scans the full journal",
    )
    parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=0,
        help="optional total read budget; 0 scans the full journal",
    )
    args = parser.parse_args()
    if args.candles:
        if not args.strategy:
            parser.error("--candles requires at least one --strategy")
        candles = normalize_canonical_candles(load_evidence_frame(args.candles))
        try:
            context_candles = _load_context_arguments(args.context_candles)
        except ValueError as exc:
            parser.error(str(exc))
        if args.quotes:
            quotes = load_evidence_frame(args.quotes)
            evidence_start = (
                pd.to_datetime(args.evidence_start, utc=True).to_pydatetime()
                if args.evidence_start
                else None
            )
            evidence_end = (
                pd.to_datetime(args.evidence_end, utc=True).to_pydatetime()
                if args.evidence_end
                else None
            )
            runtime_start = (
                pd.to_datetime(args.runtime_start, utc=True).to_pydatetime()
                if args.runtime_start
                else None
            )
            quote_replays = [
                replay_quote_scanner(
                    strategy_id,
                    candles,
                    quotes,
                    symbol=args.symbol,
                    exchange_id=args.exchange_id,
                    evidence_start=evidence_start,
                    evidence_end=evidence_end,
                    runtime_start=runtime_start,
                    context_candles=context_candles,
                )
                for strategy_id in args.strategy
            ]
            payload: dict[str, Any] = {
                "schema_version": 1,
                "generated_at": datetime.now(UTC).isoformat(),
                "read_only": True,
                "quote_replays": quote_replays,
            }
            if args.journal:
                effective_bytes = None
                if args.max_bytes_per_journal > 0:
                    effective_bytes = args.max_bytes_per_journal
                    if args.max_total_bytes > 0:
                        effective_bytes = min(
                            effective_bytes,
                            max(1, args.max_total_bytes // max(1, len(args.journal))),
                        )
                live_records = list(
                    iter_journal_records(
                        args.journal,
                        max_bytes_per_journal=effective_bytes,
                    )
                )
                payload["live_parity"] = [
                    compare_quote_replay_to_live(replay, live_records)
                    for replay in quote_replays
                ]
            atomic_write(args.out, payload)
            return
        atomic_write(args.out, {
            "schema_version": 2,
            "net_bps_semantics": "booked_execution",
            "generated_at": datetime.now(UTC).isoformat(),
            "read_only": True,
            "replays": [
                replay_scanner(
                    strategy_id,
                    candles,
                    exchange_id=args.exchange_id,
                    context_candles=context_candles,
                )
                for strategy_id in args.strategy
            ],
        })
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
    "compare_quote_replay_to_live", "iter_journal_records",
    "load_evidence_frame", "normalize_canonical_candles",
    "read_journal_records", "read_lane_evals",
    "replay_quote_scanner", "replay_scanner",
]
