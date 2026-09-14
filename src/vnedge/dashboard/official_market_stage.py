"""Source-bound official-history stages and append-only observation history.

Backfilled transitions are reconstructed descriptions, not historical live
decisions, training labels, or evidence of profitable trading performance.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime
from typing import Any

from vnedge.dashboard.analyst_history import SOURCE, normalize, validate_scope
from vnedge.dashboard.analyst_store import AnalystStore, digest, utc
from vnedge.dashboard.market_stage import SPEC as CANONICAL_SPEC, describe_stage, empty_stage
from vnedge.data.candles import TF_SECONDS

SPEC = {**CANONICAL_SPEC, "version": "market_stage_official_delta_v1",
        "source": SOURCE, "history": "reconstructed_from_receipt_time_snapshot",
        "observation_history": "append_only_reports_not_execution_events",
        "watch_rules": "same_conditions_as_classifier_not_range_break_requirement"}
SPEC_HASH = digest(SPEC)
KIND = "official_market_stage_v1"
logger = logging.getLogger(__name__)


def unavailable(tf: str, reason: str) -> dict[str, Any]:
    return {**empty_stage(tf, reason), "version": SPEC["version"], "spec_hash": SPEC_HASH,
            "source": SOURCE, "history_kind": "reconstructed", "collected_at": None}


def classify(record: dict[str, Any], symbol: str, tf: str, now: datetime) -> dict[str, Any]:
    validate_scope("delta_india", symbol, tf)
    if tf not in SPEC["timeframes"]:
        raise ValueError("unsupported_official_stage_timeframe")
    body = record["body"]
    if not utc(record["available_at"]) <= now or body["request_end"] > now.timestamp():
        return unavailable(tf, "official_stage_future_evidence")
    if (body["source"], body["symbol"], body["timeframe"]) != (SOURCE, symbol, tf):
        return unavailable(tf, "official_stage_identity_mismatch")
    rows = normalize(body["raw"], symbol, tf, body["request_end"])
    if not rows:
        return unavailable(tf, "official_stage_history_empty")
    suffix = [rows[-1]]
    for row in reversed(rows[:-1]):
        if row["close_time"] != suffix[0]["open_time"]:
            break
        suffix.insert(0, row)
    if len(suffix) < SPEC["minimum_bars"]:
        result = unavailable(tf, "need_60_contiguous_official_stage_bars")
        result["contiguous_bars"] = len(suffix)
        return result
    result = describe_stage(suffix, "delta_india", symbol, tf, now,
                            "official_stage_history_gap" if len(rows) != len(suffix) else None,
                            spec=SPEC)
    result.update({"source": SOURCE, "symbol": symbol, "exchange": "delta_india",
                   "history_kind": "reconstructed", "collected_at": record["available_at"],
                   "source_evidence_id": record["evidence_id"], "contiguous_bars": len(suffix)})
    result["stage_id"] = digest([result["stage_id"], record["evidence_id"]])
    # Existing canonical v1 wording is frozen. This independent version states
    # its actual classification criteria, rather than inventing a breakout gate.
    result["watch"] = [
        {"toward": "advancing_trend", "level": result["metrics"]["ema50"],
         "condition": f"Two consecutive closed {tf} bars each above their EMA50, with five-bar EMA slope/ATR > 0.15 and 20-bar displacement/ATR >= 2."},
        {"toward": "declining_trend", "level": result["metrics"]["ema50"],
         "condition": f"Two consecutive closed {tf} bars each below their EMA50, with five-bar EMA slope/ATR < -0.15 and 20-bar displacement/ATR <= -2."},
        {"toward": "base_or_range", "level": result["metrics"]["ema50"],
         "condition": "Two consecutive closes with absolute EMA slope/ATR <= 0.15, absolute 20-bar displacement/ATR <= 1 and prior 20-bar range width/ATR <= 6. Prior confirmed decline gives base-after-decline; prior advance gives range-after-advance; absent memory stays unknown."},
    ]
    result["note"] = "Official OHLC reconstruction, available only when collected. Accumulation/distribution are hypotheses; no demonstrated trading edge. Rolling bounded memory can revise reconstructed transitions."
    return result


def record_stage(store: AnalystStore, symbol: str, tf: str, now: datetime) -> bool:
    scope = f"delta_india/{symbol}/{tf}"
    sources = store.read("official_series", f"{symbol}/{tf}", now=now)
    if not sources:
        return False
    source = sources[0]
    previous = store.read(KIND, scope, now=now)
    if previous and previous[0]["body"]["source_evidence_id"] == source["evidence_id"]:
        return False
    stage = classify(source, symbol, tf, now)
    event = "baseline"
    if previous:
        old = previous[0]["body"]["stages"][0]
        if not stage["as_of"] or not old["as_of"]:
            event = "coverage_change"
        elif stage["as_of"] <= old["as_of"]:
            event = "source_revision"
        elif (utc(stage["as_of"]) - utc(old["as_of"])).total_seconds() != TF_SECONDS[tf]:
            event = "observation_gap"
        elif stage["stage"] != old["stage"]:
            event = "reported_stage_changed"
        else:
            event = "stage_unchanged"
    store.append(KIND, scope,
                 {"version": SPEC["version"], "source_evidence_id": source["evidence_id"],
                  "stages": [stage], "event": event,
                  "previous_report_id": previous[0]["evidence_id"] if previous else None,
                  "can_trade": False, "can_promote": False}, now)
    return True


def current_stage(store: AnalystStore, symbol: str, tf: str, now: datetime) -> dict[str, Any]:
    try:
        records = store.read(KIND, f"delta_india/{symbol}/{tf}", now=now)
        if not records:
            return unavailable(tf, "official_stage_report_pending")
        sources = store.read("official_series", f"{symbol}/{tf}", now=now)
        if not sources or sources[0]["evidence_id"] != records[0]["body"]["source_evidence_id"]:
            return unavailable(tf, "official_stage_source_changed_report_pending")
        result = copy.deepcopy(records[0]["body"]["stages"][0])
        if result["spec_hash"] != SPEC_HASH or result.get("source") != SOURCE:
            return unavailable(tf, "official_stage_contract_mismatch")
        if result["as_of"]:
            age = (now - utc(result["as_of"])).total_seconds()
            result["state"] = "current" if 0 <= age <= TF_SECONDS[tf]*1.5 else "stale"
        result["report_id"] = records[0]["evidence_id"]
        result["recorded_at"] = records[0]["available_at"]
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("official_stage_read_failed %s/%s %s", symbol, tf, type(exc).__name__)
        return unavailable(tf, "official_stage_evidence_invalid")


def history(store: AnalystStore, symbol: str, now: datetime) -> dict[str, Any]:
    validate_scope("delta_india", symbol)
    try:
        reports = [r for tf in SPEC["timeframes"] for r in store.read(
            KIND, f"delta_india/{symbol}/{tf}", now=now, limit=30)]
        reports.sort(key=lambda r: (r["available_at"], r["evidence_id"]), reverse=True)
        return {"reports": [], "changes": [], "stage_reports": reports[:30],
                "status": "recorded" if reports else "no_saved_reports",
                "history_kind": "official_observation_reports_with_reconstructed_transitions",
                "can_trade": False, "can_promote": False}
    except Exception:  # noqa: BLE001
        return {"reports": [], "changes": [], "stage_reports": [],
                "status": "evidence_read_failed", "can_trade": False, "can_promote": False}
