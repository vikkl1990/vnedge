"""Read-only, bounded journal index. Never an order authority or a PnL book.

Only this disposable SQLite projection is writable. Source journals are opened
read-only; no historical candle scan, scanner call, or execution import occurs.
Call from a worker thread, never the HTTP/scanner event loop.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from vnedge.execution.evidence import DecisionEnvelope
from vnedge.risk.risk_manager import OrderIntent

KINDS = frozenset({
    "lane_eval", "decision_armed", "candidate_evaluation", "shadow_intent", "shadow_outcome",
    "scalp_shadow_intent", "scalp_shadow_outcome", "cost_rejected",
    "sizing_rejected", "entry_evidence_rejected", "entry_quote_rejected",
    "entry_route_rejected", "shadow_entry_blocked",
    "risk_decision", "order_intent", "order_submitted", "order_acknowledged",
    "order_fill_sync", "order_resolved", "order_timeout_unknown",
    "order_rejected", "order_refused", "order_cancel", "order_ack_race_resolved", "reconciling", "live_paper_exit",
    "paper_exit", "tick_stop_exit", "private_order_update",
})
RESEARCH = {"shadow_intent", "shadow_outcome", "scalp_shadow_intent", "scalp_shadow_outcome"}
REJECTS = {"cost_rejected", "sizing_rejected", "entry_evidence_rejected",
           "entry_quote_rejected", "entry_route_rejected",
           "shadow_entry_blocked", "order_rejected", "order_refused"}
FILTERS = ("lane", "strategy_id", "symbol", "timeframe", "entry_clock", "mode", "stage", "population")
PROJECTION_VERSION = "2"  # direct decision_armed envelopes and post-ARM rejects


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str, allow_nan=False).encode()).hexdigest()


def obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def text(value: Any) -> str | None:
    return value[:1000] if isinstance(value, str) and value else None


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def timestamp(value: Any) -> str | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(UTC).isoformat() if dt.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def normalize(lane: str, record: dict) -> dict | None:
    """Allowlisted public fields only. Invalid proof never acquires a decision ID."""
    kind, p = record.get("kind"), obj(record.get("payload"))
    if kind not in KINDS or p.get("backfill") is True:
        return None
    sig, ev, intent = obj(p.get("signal")), obj(p.get("execution_evidence")), obj(p.get("intent"))
    envelope = None
    error = None
    # The runner journals the ARM envelope itself as the decision_armed
    # payload. Other lifecycle records carry it in arm_envelope. Treat the
    # direct payload as proof only after the same strict identity validation.
    containers = ({"arm_envelope": p}, p, sig, ev) if kind == "decision_armed" else (p, sig, ev)
    for container in containers:
        if container.get("arm_envelope") is None:
            continue
        try:
            candidate = DecisionEnvelope.from_dict(obj(container["arm_envelope"]))
            for part in (p, sig, ev):
                for field, expected in (("decision_id", candidate.decision_id),
                                        ("snapshot_id", candidate.snapshot_id),
                                        ("htf_snapshot_id", candidate.snapshot_id),
                                        ("path_id", candidate.path_id),
                                        ("entry_clock", candidate.entry_clock),
                                        ("decision_bar_content_hash", candidate.decision_bar_content_hash),
                                        ("strategy_id", candidate.strategy_id),
                                        ("symbol", candidate.symbol),
                                        ("timeframe", candidate.timeframe),
                                        ("side", candidate.side)):
                    if part.get(field) not in (None, "", expected):
                        raise ValueError("identity mismatch")
            if intent.get("symbol") not in (None, "", candidate.symbol):
                raise ValueError("intent symbol mismatch")
            if intent.get("reduce_only") is not True and intent.get("side") not in (None, "", candidate.side):
                raise ValueError("intent side mismatch")
            if envelope is not None and candidate != envelope:
                raise ValueError("conflicting envelopes")
            envelope = candidate
        except (ValueError, KeyError, TypeError, AttributeError):
            error = "invalid_arm_envelope"
    if error:
        envelope = None
    identifier = digest([lane, record])
    failures = list(dict.fromkeys(
        x[:300] for field in ("failed_checks", "failed_gates", "all_failed_gates")
        if isinstance(p.get(field), list)
        for x in p[field][:50] if isinstance(x, str)
    ))
    reason = next((text(p.get(k)) for k in ("primary_failed_gate", "skip_reason", "reason", "explanation", "resolution") if text(p.get(k))), None)
    from vnedge.strategy.decision_context import explain_evaluation

    context = explain_evaluation(p).to_dict() if kind == "lane_eval" else None
    proof = envelope.as_dict() if envelope else None
    cost = obj(ev.get("cost_decision"))
    intent_hash = None
    if intent:
        try:
            instruction = OrderIntent(**{f.name: intent[f.name] for f in fields(OrderIntent) if f.name in intent})
            intent_hash = digest(asdict(instruction))
        except (ValueError, TypeError):
            pass
    return {
        "event_id": identifier, "lane": lane, "kind": kind,
        "observed_at": timestamp(record.get("ts")),
        "decision_open": envelope.bar_open.isoformat() if envelope else timestamp(p.get("bar_ts")),
        "decision_close": envelope.permission_snapshot.decision_bar.close_time.isoformat() if envelope else timestamp(p.get("decision_at")),
        "decision_id": envelope.decision_id if envelope else None,
        "claimed_decision_id": text(p.get("decision_id") or ev.get("decision_id")),
        "envelope": proof, "evidence_error": error,
        "strategy_id": envelope.strategy_id if envelope else text(p.get("strategy_id") or intent.get("strategy_id")),
        "symbol": envelope.symbol if envelope else text(p.get("symbol") or intent.get("symbol")),
        "timeframe": envelope.timeframe if envelope else text(p.get("timeframe")),
        "entry_clock": envelope.entry_clock if envelope else text(p.get("entry_clock")),
        "side": envelope.side if envelope else text(sig.get("side") or p.get("side") or intent.get("side")),
        "exchange": text(p.get("exchange")), "mode": text(p.get("mode") or intent.get("mode")),
        "client_order_id": text(p.get("client_order_id")),
        "intent_hash": intent_hash,
        "entry": number(p.get("entry_price") or intent.get("limit_price")),
        "stop": number(sig.get("stop_price") or p.get("stop_price") or intent.get("stop_price")),
        "target": number(sig.get("take_profit_price") or p.get("take_profit_price")),
        "filled_quantity": number(p.get("filled_quantity")),
        "order_state": text(p.get("state") or p.get("venue_state")),
        "approved": p.get("approved") if isinstance(p.get("approved"), bool) else None,
        "fired": p.get("fired") is True, "failures": failures, "reason": reason,
        "decision_context": context,
        "cost_profile_id": text(cost.get("cost_profile_id") or p.get("cost_profile_id")),
        "quote_sequence": (ev["quote_sequence"] if type(ev.get("quote_sequence")) is int
                           else text(ev.get("quote_sequence"))),
        "bbo_ts": timestamp(ev.get("bbo_ts")), "quote_age_ms": number(ev.get("quote_age_ms")),
        # These are research-only numbers. No operational net is reconstructed
        # from exits here: a full entry/exit fee/funding join belongs to Book.
        "research_net_usd": number(p.get("virtual_net_usd")) if kind in RESEARCH else None,
    }


def project(events: list[dict]) -> list[dict]:
    """Fold exact identities; do not join by approximate time, side or symbol."""
    groups: dict[str, list[dict]] = {}
    conflicted = {(e["lane"], e["claimed_decision_id"]) for e in events if e["evidence_error"] and e["claimed_decision_id"]}
    clients: dict[tuple[str, str], list[dict]] = {}
    for event in events:
        if event["client_order_id"] and event["decision_id"]:
            clients.setdefault((event["lane"], event["client_order_id"]), []).append(event)
    for linked in clients.values():
        if len({e["decision_id"] for e in linked}) > 1 or len({e["intent_hash"] for e in linked if e["intent_hash"]}) > 1:
            conflicted.update((e["lane"], e["decision_id"]) for e in linked)
    for e in events:
        key = digest(["decision", e["lane"], e["decision_id"]]) if e["decision_id"] else e["event_id"]
        groups.setdefault(key, []).append(e)
    rows = []
    for key, timeline in groups.items():
        timeline.sort(key=lambda e: (e["observed_at"] or "", e.get("source_offset", 0)))
        bound = next((e for e in timeline if e["envelope"]), None)
        first, last = timeline[0], timeline[-1]
        base = bound or first
        conflict = bool(base["evidence_error"]) or (base["lane"], base["decision_id"]) in conflicted
        orders: dict[str, dict] = {}
        failures: list[str] = []
        stage = "armed" if bound else "evaluated"
        for e in timeline:
            failures.extend(e["failures"])
            if e["kind"] in REJECTS or e["approved"] is False:
                stage = "rejected"
            elif e["kind"] == "risk_decision" and e["approved"] is True and bound:
                stage = "risk_approved"
            elif e["kind"] == "candidate_evaluation" and e["approved"] is True and bound:
                stage = "candidate_approved"
            if e["client_order_id"] and e["decision_id"] and (e["kind"].startswith("order_") or e["kind"] in {"reconciling", "private_order_update"}):
                order = orders.setdefault(e["client_order_id"], {"client_order_id": e["client_order_id"],
                    "intent_recorded": False, "submitted": False, "filled_quantity": 0.0, "state": "unknown"})
                if e["kind"] == "order_intent":
                    order["intent_recorded"] = bool(e["intent_hash"])
                if e["kind"] == "order_submitted":
                    order["submitted"] = bool(e["intent_hash"])
                    order["state"] = "submitted"
                if e["kind"] == "order_acknowledged":
                    order["state"] = "acknowledged"
                if e["kind"] in {"order_fill_sync", "order_resolved", "order_cancel", "order_ack_race_resolved", "private_order_update"}:
                    order["filled_quantity"] = max(order["filled_quantity"], e["filled_quantity"] or 0.0)
                    order["state"] = (e["order_state"] or "unknown").lower()
                if e["kind"] in {"order_timeout_unknown", "order_rejected"}:
                    order["state"] = e["kind"].removeprefix("order_")
                if e["kind"] == "reconciling":
                    order["state"] = "reconciling"
        operational = [o for o in orders.values() if o["intent_recorded"] and o["submitted"]]
        filled = any(o["filled_quantity"] > 0 for o in operational) and not conflict
        if orders and not operational:
            stage = "intent_recorded" if all(o["intent_recorded"] and not o["submitted"] and o["filled_quantity"] == 0 for o in orders.values()) else "incomplete_order_chain"
        if operational:
            states = {o["state"] for o in operational}
            stage = ("timeout_unknown" if "timeout_unknown" in states else
                     "reconciling" if "reconciling" in states else
                     "canceled_partial" if filled and states <= {"cancelled", "canceled"} else
                     "filled" if filled and states <= {"filled"} else
                     "partially_filled" if filled else
                     "acknowledged" if "acknowledged" in states else
                     "rejected" if states <= {"rejected"} else
                     "canceled" if states <= {"cancelled", "canceled"} else "order_state_unknown" if "unknown" in states else "submitted")
        research = any(e["kind"] in RESEARCH for e in timeline)
        if not bound and (first["fired"] or first["evidence_error"] or first["kind"].startswith("order_") or first["kind"] == "private_order_update"):
            stage = "identity_gap"
        if conflict:
            stage = "identity_gap"
        population = "orders" if operational and not conflict else "research" if research else "decisions" if bound else "evaluations"
        if research and not operational and not conflict:
            stage = "research_resolved" if any(e["kind"].endswith("outcome") for e in timeline) else stage
        latest_value = lambda name: next((e[name] for e in reversed(timeline) if e.get(name) is not None), None)
        rows.append({
            "row_key": key, "lane": base["lane"], "decision_id": base["decision_id"],
            "evaluation_id": None if bound else "diagnostic:" + key,
            "snapshot_id": base["envelope"]["snapshot_id"] if bound else None,
            **{name: base.get(name) for name in ("strategy_id", "symbol", "timeframe", "entry_clock", "side", "decision_open", "decision_close")},
            "exchange": latest_value("exchange"), "mode": latest_value("mode"),
            "observed_at": first["observed_at"], "last_event_at": last["observed_at"],
            "stage": stage, "population": population, "has_fill": filled,
            "evidence_status": "conflict" if conflict else "bound" if bound else "unbound",
            "primary_reason": latest_value("reason"), "failed_gates": list(dict.fromkeys(failures)),
            "decision_context": latest_value("decision_context"),
            "entry": latest_value("entry"), "stop": latest_value("stop"), "target": latest_value("target"),
            "cost_profile_id": latest_value("cost_profile_id"),
            "ml_status": "not_recorded", "ml_probability": None,
            "outcome_basis": "research_observation" if research and not operational else "order_journal" if operational else "no_execution",
            "research_net_usd": latest_value("research_net_usd") if research and not operational else None,
            "booked_net_usd": None, "performance_eligible": False,
            "envelope": base["envelope"], "orders": list(orders.values()), "timeline": timeline,
        })
    return sorted(rows, key=lambda r: (r["last_event_at"] or "", r["row_key"]), reverse=True)


class SignalQueue:
    """Incremental, single-writer disposable index with strict bounded reads.

    Bootstraps from the latest bytes, then tails complete lines. Never claims
    full-history coverage; evicted ancestors leave descendants unproven.
    """
    def __init__(self, root: Path | None, *, read_bytes: int = 524288, max_events: int = 2000,
                 index_path: Path | None = None) -> None:
        self.root = Path(root) if root else None
        self.index_path = index_path
        self.read_bytes, self.max_events = read_bytes, max_events
        self.lock = threading.Lock()
        self.cached: dict | None = None
        self.cached_at = 0.0
        self.active_key: tuple[str, ...] = ()

    def snapshot(self, runtime: dict | None) -> dict:
        lanes = obj(runtime).get("lanes") or []
        active = sorted({r["lane_id"] for r in lanes if isinstance(r, dict)
                         and isinstance(r.get("lane_id"), str)
                         and re.fullmatch(r"[\w.-]{1,240}", r["lane_id"])
                         and not r["lane_id"].startswith("measurement_")})
        with self.lock:
            if self.cached and time.monotonic() - self.cached_at < 5 and tuple(active) == self.active_key:
                return self.cached
            statuses, events = [], []
            if self.root and self.root.is_dir() and active:
                with closing(sqlite3.connect(self.index_path or self.root / ".dashboard_signal_queue_v1.sqlite", timeout=2)) as db, db:
                    db.execute("CREATE TABLE IF NOT EXISTS sources (lane TEXT PRIMARY KEY, inode TEXT, offset INTEGER, anchor TEXT)")
                    db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, lane TEXT, offset INTEGER, body TEXT)")
                    db.execute("CREATE INDEX IF NOT EXISTS events_lane ON events(lane, offset)")
                    db.execute("CREATE TABLE IF NOT EXISTS issues (lane TEXT PRIMARY KEY, invalid_records INTEGER)")
                    db.execute("CREATE TABLE IF NOT EXISTS coverage (lane TEXT PRIMARY KEY, skipped_bytes INTEGER, reason TEXT)")
                    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                    version = db.execute("SELECT value FROM meta WHERE key='projection_version'").fetchone()
                    if version is None or version[0] != PROJECTION_VERSION:
                        # This SQLite file is a disposable journal projection.
                        # A parser upgrade must re-read the bounded source tail;
                        # otherwise an existing offset would hide newly indexed
                        # ARM events until another decision happened.
                        for table in ("sources", "events", "issues", "coverage"):
                            db.execute(f"DELETE FROM {table}")
                        db.execute("INSERT OR REPLACE INTO meta VALUES('projection_version', ?)",
                                   (PROJECTION_VERSION,))
                    for lane in active[:32]:
                        statuses.append(self._ingest(db, lane))
                        events.extend(json.loads(row[0]) for row in db.execute(
                            "SELECT body FROM events WHERE lane=? ORDER BY offset", (lane,)))
            rows = project(events)
            now = datetime.now(UTC).isoformat()
            self.cached = {"generated_at": now, "last_event_at": max((r["last_event_at"] or "" for r in rows), default=None),
                           "revision": digest([rows, statuses]), "rows": rows, "sources": statuses,
                           "history_complete": False, "coverage": "bounded_recent_journals",
                           "active_lanes_truncated": len(active) > 32, "can_trade": False, "can_promote": False}
            self.cached_at, self.active_key = time.monotonic(), tuple(active)
            return self.cached

    def _ingest(self, db: sqlite3.Connection, lane: str) -> dict:
        assert self.root is not None
        path = self.root / f"{lane}.journal.jsonl"
        status = {"lane": lane, "state": "ok", "invalid_records": 0, "caught_up": False}
        prior_issues = db.execute("SELECT invalid_records FROM issues WHERE lane=?", (lane,)).fetchone()
        if prior_issues:
            status["invalid_records"] = prior_issues[0]
        coverage = db.execute("SELECT skipped_bytes, reason FROM coverage WHERE lane=?", (lane,)).fetchone()
        status.update(skipped_bytes=coverage[0] if coverage else 0,
                      resync_reason=coverage[1] if coverage else None)
        if path.is_symlink():
            return {**status, "state": "symlink_refused"}
        try:
            with path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                inode = f"{stat.st_dev}:{stat.st_ino}"
                old = db.execute("SELECT inode, offset, anchor FROM sources WHERE lane=?", (lane,)).fetchone()
                reset = not old or old[0] != inode or stat.st_size < old[1]
                if old and not reset:
                    handle.seek(max(0, old[1] - 128))
                    reset = hashlib.sha256(handle.read(min(128, old[1]))).hexdigest() != old[2]
                # This is a recent-window display, not an archival consumer.
                # After a quiet UI, replaying a large backlog per HTTP poll can
                # leave the display hours behind while journals are healthy.
                lagged = bool(old and not reset and stat.st_size - old[1] > self.read_bytes)
                reset = reset or lagged
                offset = old[1] if old and not reset else max(0, stat.st_size - self.read_bytes)
                if reset:
                    db.execute("DELETE FROM events WHERE lane=?", (lane,))
                    status["invalid_records"] = 0
                    previous_offset = old[1] if lagged else 0
                    handle.seek(offset)
                    if offset:
                        handle.readline(self.read_bytes)
                        offset = handle.tell()
                    # Persist the coverage gap even on subsequent caught-up
                    # polls. Evicted order ancestors must never prove fills.
                    status["skipped_bytes"] = (status["skipped_bytes"] if lagged else 0) + max(0, offset - previous_offset)
                    status["resync_reason"] = "backlog_tail_resync" if lagged else "recent_tail_bootstrap"
                    db.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?)",
                               (lane, status["skipped_bytes"], status["resync_reason"]))
                handle.seek(offset)
                chunk = handle.read(min(self.read_bytes, max(0, stat.st_size - offset)))
                complete = chunk.rfind(b"\n") + 1
                for line in chunk[:complete].splitlines(keepends=True):
                    start = offset
                    offset += len(line)
                    try:
                        record = json.loads(line)
                        event = normalize(lane, record) if isinstance(record, dict) else None
                        if event:
                            event["source_offset"] = start
                            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?)",
                                       (event["event_id"], lane, start, json.dumps(event, allow_nan=False)))
                    except (ValueError, TypeError, OverflowError):
                        status["invalid_records"] += 1
                if not complete and len(chunk) == self.read_bytes:
                    status["state"] = "oversize_record_operator_review"
                handle.seek(max(0, offset - 128))
                anchor = hashlib.sha256(handle.read(min(128, offset))).hexdigest()
                db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?)", (lane, inode, offset, anchor))
                db.execute("INSERT OR REPLACE INTO issues VALUES(?,?)", (lane, status["invalid_records"]))
                db.execute("DELETE FROM events WHERE lane=? AND id NOT IN (SELECT id FROM events WHERE lane=? ORDER BY offset DESC LIMIT ?)",
                           (lane, lane, self.max_events))
                status["caught_up"] = offset == stat.st_size
                status["remaining_bytes"] = max(0, stat.st_size - offset)
        except OSError:
            status["state"] = "source_unavailable"
        return status


def page(snapshot: dict, *, filters: dict[str, str], limit: int = 25, cursor: str = "") -> dict:
    if not 1 <= limit <= 100 or any(k not in FILTERS for k in filters):
        raise ValueError("invalid queue filter or limit")
    signature, offset = digest([filters, limit]), 0
    if cursor:
        try:
            if len(cursor) > 512:
                raise ValueError()
            rev, match, offset = json.loads(base64.urlsafe_b64decode(cursor))
            if match != signature or type(offset) is not int or offset < 0:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("invalid queue cursor") from None
        if rev != snapshot["revision"]:
            raise RuntimeError("queue_changed_refresh_first_page")
    rows = [r for r in snapshot["rows"] if all(
        not v or (r["population"] in {"decisions", "orders"} if k == "population" and v == "decisions" else r.get(k) == v)
        for k, v in filters.items())]
    selected = rows[offset:offset + limit]
    summary = {"total": len(rows), "decisions": sum(r["decision_id"] is not None for r in rows),
               "with_fills": sum(r["has_fill"] for r in rows),
               "rejected": sum(r["stage"] == "rejected" for r in rows),
               "identity_gaps": sum(r["stage"] == "identity_gap" for r in rows)}
    next_cursor = base64.urlsafe_b64encode(json.dumps([snapshot["revision"], signature, offset + limit]).encode()).decode() if offset + limit < len(rows) else None
    return {**{k: v for k, v in snapshot.items() if k != "rows"}, "summary": summary,
            "facets": {k: sorted({r[k] for r in snapshot["rows"] if r.get(k)})
                       for k in ("strategy_id", "symbol", "entry_clock", "mode")},
            "rows": [{k: v for k, v in row.items() if k not in {"timeline", "envelope", "orders"}} for row in selected],
            "next_cursor": next_cursor}
