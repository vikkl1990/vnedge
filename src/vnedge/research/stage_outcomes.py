"""Receipt-time Analyst context beside scanner journals, never a signal input.

The journal index is bounded and diagnostic. Only the independent hash-chain
and accounting verifier can supply outcomes. Official stage snapshots remain
official; they do not become canonical decision evidence by being attached.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import math
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vnedge.dashboard.analyst_history import DEFAULT_PATH as OFFICIAL_PATH, SYMBOLS, validate_scope
from vnedge.dashboard.analyst_store import AnalystStore, digest, utc
from vnedge.dashboard.official_market_stage import current_stage
from vnedge.dashboard.signal_queue import SignalQueue
from vnedge.ml.lane_inventory import lane_inventory
from vnedge.ml.ledger_labels import build_ledger_labels

DEFAULT_PATH = Path("data/analyst_stage/evidence.sqlite")
VERSION = "scanner_stage_outcomes_v1"
BINDING = "scanner_stage_binding_v1"
AUTHORITY = {"can_trade": False, "can_promote": False, "performance_eligible": False}
IDENTITY = ("strategy_id", "exchange", "symbol", "timeframe", "entry_clock", "mode")
COHORT = (*IDENTITY, "cost_profile_id", "cost_config_sha256", "label_contract")
ANCHORS = {"lane_eval", "candidate_evaluation", "order_intent", "shadow_intent", "scalp_shadow_intent"}
log = logging.getLogger(__name__)


def attach_lane_metadata(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order instructions omit venue/mode; use a prior exact-bar lane evaluation.

    Never infer these from lane filenames, later fills, or another market/time.
    This is metadata only, not a decision-ID join or a source of PnL.
    """
    evaluations: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    keys = ("lane", "strategy_id", "symbol", "timeframe", "entry_clock", "decision_close")
    for row in rows:
        for event in row["timeline"]:
            if event["kind"] == "lane_eval" and event.get("exchange") and event.get("mode"):
                evaluations[tuple(event.get(k) for k in keys)].append(event)
    result = []
    for row in rows:
        row = {**row, "timeline": [dict(e) for e in row["timeline"]]}
        for event in row["timeline"]:
            prior = [e for e in evaluations[tuple(event.get(k) for k in keys)]
                     if e.get("observed_at") and event.get("observed_at") and e["observed_at"] <= event["observed_at"]]
            identities = {(e["exchange"], e["mode"]) for e in prior}
            if len(identities) == 1:
                exchange, mode = next(iter(identities))
                if event.get("exchange") in (None, exchange) and event.get("mode") in (None, mode):
                    event.update(exchange=exchange, mode=mode, metadata_event_id=prior[-1]["event_id"])
                    if row.get("exchange") in (None, exchange) and row.get("mode") in (None, mode):
                        row.update(exchange=exchange, mode=mode)
        result.append(row)
    return result


def market(symbol: str, exchange: str) -> str:
    if exchange != "delta_india":
        raise ValueError("unsupported_exchange")
    names = {s: s for s in SYMBOLS}
    names.update({s[:-3] + "/USD:USD": s for s in SYMBOLS})
    if symbol not in names:
        raise ValueError("unsupported_contract")
    return names[symbol]


def bind(store: AnalystStore, official: AnalystStore, row: dict[str, Any],
         activated_at: datetime, now: datetime) -> tuple[dict[str, Any] | None, str]:
    """Write once per exact evaluation/decision key, including negative evidence."""
    if row.get("evidence_status") == "conflict":
        return None, "identity_conflict"
    try:
        symbol = market(row["symbol"], row["exchange"])
        anchors = [e for e in row["timeline"] if e["kind"] in ANCHORS]
        if not anchors:
            return None, "entry_anchor_missing"
        anchor = min(anchors, key=lambda e: utc(e["observed_at"]))
        cutoff, observed = utc(row["decision_close"]), utc(anchor["observed_at"])
        if cutoff < activated_at:
            return None, "before_activation"
        if not cutoff <= observed <= now:
            return None, "invalid_decision_time"
        # Never obtain identity from a later order/exit event in the projection.
        if any(row.get(k) != anchor.get(k) for k in IDENTITY):
            return None, "anchor_identity_incomplete"
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, "unsupported_or_incomplete_identity"
    previous = store.read(BINDING, row["row_key"], now=now)
    if previous:
        return previous[0], "already_bound"
    stage_fields = ("timeframe", "stage", "state", "version", "source", "spec_hash", "stage_id",
                    "as_of", "collected_at", "recorded_at", "report_id", "source_evidence_id", "issues")
    stages = [{k: s.get(k) for k in stage_fields}
              for tf in ("4h", "1d") for s in [current_stage(official, symbol, tf, cutoff)]]
    body = {"version": VERSION, "row_key": row["row_key"], "lane": row["lane"],
            **{k: row.get(k) for k in IDENTITY}, "market": symbol,
            "decision_id": row["decision_id"], "evaluation_id": row["evaluation_id"],
            "envelope": row.get("envelope"), "anchor_event_id": anchor["event_id"],
            "metadata_event_id": anchor.get("metadata_event_id"),
            "cutoff": cutoff.isoformat(), "observed_at": observed.isoformat(),
            "stages": stages, "binding_basis": "available_by_decision_close",
            **AUTHORITY}
    store.append(BINDING, row["row_key"], body, now)
    return store.read(BINDING, row["row_key"], now=now)[0], "captured"


def match_outcomes(store: AnalystStore, lane: str, audit: dict[str, Any], now: datetime
                   ) -> tuple[list[dict[str, Any]], Counter[str]]:
    issues: Counter[str] = Counter()
    if audit.get("state") != "VERIFIED":
        return [], Counter({"ledger_not_verified": 1})
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label in audit.get("labels", []):
        try:
            if label["label_hash"] != digest({k: v for k, v in label.items() if k != "label_hash"}):
                raise ValueError("label_hash_mismatch")
            if not utc(label["entry_at"]) <= utc(label["exit_at"]) <= utc(label["available_at"]) <= now:
                raise ValueError("label_time_invalid")
            if any(not label.get(k) for k in COHORT) or label["mode"] != "paper":
                raise ValueError("label_contract_missing")
            if any(not math.isfinite(float(label[k])) for k in ("net_bps", "net_usd", "gross_usd", "fees_usd", "funding_usd")):
                raise ValueError("label_nonfinite")
            key = digest(["decision", lane, label["decision_id"]])
            if key in seen:
                # Ambiguous episodes cannot double count or select a winner.
                raise ValueError("duplicate_decision_outcomes")
            seen.add(key)
            records = store.read(BINDING, key, now=now)
            if not records:
                issues["no_forward_stage_binding"] += 1
                continue
            record, context = records[0], records[0]["body"]
            if any(context.get(k) != label[k] for k in IDENTITY) or context["envelope"] != label["arm_envelope"]:
                raise ValueError("outcome_identity_mismatch")
            if utc(context["cutoff"]) > utc(label["entry_at"]):
                raise ValueError("context_after_entry")
            result = {"lane": lane, "context": context, "binding_id": record["evidence_id"],
                      "label": label, "ledger_source_hash": audit.get("source_hash"), **AUTHORITY}
            matched.append(result)
        except (KeyError, TypeError, ValueError) as exc:
            # Any contradictory accounting/identity invalidates the lane report.
            return [], Counter({str(exc): 1})
    return matched, issues


def aggregate(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identities: dict[str, dict[str, Any]] = {}
    for result in outcomes:
        label = result["label"]
        for stage in [{"timeframe": "all", "stage": "all_bound_outcomes", "version": "all", "source": "all", "spec_hash": "all"}, *result["context"]["stages"]]:
            name = stage["stage"] if stage.get("state") == "current" or stage["timeframe"] == "all" else "context_unavailable"
            identity = {"lane": result["lane"], **{k: label[k] for k in COHORT},
                        "context_contract_hash": digest([[x["timeframe"], x["version"], x["source"], x["spec_hash"]]
                                                         for x in result["context"]["stages"]]),
                        "stage_tf": stage["timeframe"], "stage": name,
                        "stage_version": stage["version"], "stage_source": stage["source"],
                        "stage_spec_hash": stage["spec_hash"]}
            key = digest(identity)
            identities[key] = identity
            groups[key].append(label)
    rows = []
    for key, labels in groups.items():
        n = len(labels)
        wins = sum(max(0, x["net_usd"]) for x in labels)
        losses = -sum(min(0, x["net_usd"]) for x in labels)
        rows.append({"group_id": key, **identities[key], "n": n,
                     "state": "INSUFFICIENT" if n < 30 else "DESCRIPTIVE_ONLY",
                     "mean_net_bps": sum(x["net_bps"] for x in labels) / n,
                     "net_usd": sum(x["net_usd"] for x in labels),
                     "profit_factor": wins/losses if n >= 30 and losses > 0 else None,
                     "funding": "included_verified_settled_cash", **AUTHORITY})
    return sorted(rows, key=lambda r: r["group_id"])


class StageWorker:
    def __init__(self, journals: Path, official: Path, output: Path) -> None:
        self.journals = journals
        self.store = AnalystStore(output, writable=True, wal=False)
        self.official = AnalystStore(official)
        self.queue = SignalQueue(journals, index_path=output.parent / "journal_index.sqlite")

    def cycle(self, now: datetime) -> dict[str, Any]:
        activation = self.store.read("activation", VERSION, now=now)
        if not activation:
            self.store.append("activation", VERSION, {"activated_at": now.isoformat(), **AUTHORITY}, now)
            activation = self.store.read("activation", VERSION, now=now)
        activated_at = utc(activation[0]["body"]["activated_at"])
        inventory = lane_inventory(self.journals)
        paths = [p for p in inventory["journals"] if not p.name.startswith("measurement_")]
        snapshot = self.queue.snapshot({"lanes": [{"lane_id": p.name.removesuffix(".journal.jsonl")} for p in paths]})
        counts: Counter[str] = Counter()
        contexts = []
        for row in attach_lane_metadata(snapshot["rows"]):
            record, reason = bind(self.store, self.official, row, activated_at, now)
            counts[reason] += 1
            if record:
                contexts.append({k: v for k, v in record["body"].items() if k != "envelope"})
        outcomes, audits = [], []
        for path in paths[:32]:
            lane = path.name.removesuffix(".journal.jsonl")
            fills = path.with_name(lane + ".fills.jsonl")
            if not fills.is_file():
                audits.append({"lane": lane, "state": "WAITING_FOR_PAPER_LEDGER", "issues": {}})
                continue
            try:
                audit = build_ledger_labels(path, fills)
                matched, issues = match_outcomes(self.store, lane, audit, now)
                for outcome in matched:
                    if not self.store.read("verified_stage_outcome", outcome["label"]["label_hash"], now=now):
                        self.store.append("verified_stage_outcome", outcome["label"]["label_hash"],
                                          {k: v for k, v in outcome.items() if k != "context"}, now)
                outcomes.extend(matched)
                audits.append({"lane": lane, "state": audit["state"],
                               "issues": {**audit.get("rejections", {}), **dict(issues)}})
            except Exception as exc:  # report failed verification; never reuse old metrics
                log.exception("stage_outcome_audit_failed %s", lane)
                audits.append({"lane": lane, "state": "BLOCKED", "issues": {type(exc).__name__: 1}})
        report = {"version": VERSION, "generated_at": now.isoformat(), "activated_at": activated_at.isoformat(),
                  "state": "current", "counts": dict(counts), "contexts": contexts[:1000],
                  "contexts_truncated": len(contexts) > 1000, "groups": aggregate(outcomes),
                  "verified_outcomes": len(outcomes), "audits": audits,
                  "sources": snapshot["sources"], "lanes_truncated": len(paths) > 32,
                  "directory_available": inventory["directory_available"],
                  "history_complete": False, "coverage": "bounded_recent_evaluations_and_verified_forward_paper_outcomes",
                  "note": "Observational stage association, not OOS evidence or a stage-filter backtest. No virtual shadow PnL. No live scanner changes.",
                  **AUTHORITY}
        self.store.append("report", VERSION, report, now)
        return report


def report(path: Path, symbol: str, exchange: str, now: datetime) -> dict[str, Any]:
    validate_scope(exchange, symbol)
    empty = {"state": "worker_not_started", "contexts": [], "groups": [], "audits": [], **AUTHORITY}
    try:
        records = AnalystStore(path).read("report", VERSION, now=now)
        if not records:
            return empty
        result = records[0]["body"]
        result["contexts"] = [c for c in result["contexts"] if c["market"] == symbol]
        result["groups"] = [g for g in result["groups"] if market(g["symbol"], g["exchange"]) == symbol]
        age = (now - utc(result["generated_at"])).total_seconds()
        result["state"] = "current" if 0 <= age <= 900 else "stale"
        return result
    except Exception:
        log.exception("stage_outcome_report_read_failed")
        return {**empty, "state": "evidence_read_failed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journals", type=Path, default=Path("logs/paper_trials"))
    parser.add_argument("--official", type=Path, default=OFFICIAL_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    with (args.output.parent / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = StageWorker(args.journals, args.official, args.output)
        while True:
            worker.cycle(datetime.now(UTC))
            if args.once:
                return
            time.sleep(60)


if __name__ == "__main__":
    main()
