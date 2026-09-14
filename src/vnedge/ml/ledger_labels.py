"""Full-prefix execution accounting for ML. Never labels an exit intention.

The bounded dashboard audit is deliberately not an input. Both ledger prefixes
are verified as read (the bytes verified are the bytes consumed). Funding
coverage is an independent receipt; absence of a payment is NOT proof of zero.
Only single-owner, flat-to-flat paper episodes are supported by this version.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from vnedge.execution.evidence import DecisionEnvelope
from vnedge.execution.fill_ledger import _record_hash
from vnedge.execution.journal import _v2_record_error


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def timestamp(value: Any) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError("timezone_required")
    return stamp.astimezone(UTC)


def money(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("invalid_number")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite_number")
    return result


def iter_records(
    path: Path, *, chain: str | None = None, max_bytes: int = 2_000_000_000,
    max_record_bytes: int = 8_000_000,
) -> Iterator[dict]:
    """Verify a fixed full prefix incrementally, including irrelevant records.

    Append-only writers may continue after the captured size. A partial record
    at that boundary, a truncated file, or any bad chain link blocks the audit.
    Consumers must exhaust the iterator before publishing any derived evidence.
    """
    if chain not in {None, "journal", "fills"}:
        raise ValueError("unknown_chain")
    if path.is_symlink():
        raise ValueError("symlink_refused")
    prev = "0" * 64
    seq = 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("regular_file_required")
        remaining = info.st_size
        if remaining > max_bytes:
            raise ValueError("full_history_limit_exceeded")
        while remaining:
            line = stream.readline(min(remaining, max_record_bytes + 1))
            if not line:
                raise ValueError("history_truncated_during_read")
            if len(line) > max_record_bytes:
                raise ValueError("record_size_limit_exceeded")
            remaining -= len(line)
            if not line.endswith(b"\n"):
                raise ValueError("incomplete_tail")
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise TypeError("invalid_record")
            digest(row)
            if chain:
                body = {k: v for k, v in row.items() if k not in {"hash", "prev_hash"}}
                valid = (not _v2_record_error(row) if chain == "journal"
                         else _record_hash(body, prev) == row.get("hash"))
                if (not valid or row.get("prev_hash") != prev
                    or type(row.get("seq")) is not int or row.get("seq") != seq):
                    raise ValueError("broken_or_legacy_chain")
                prev = row["hash"]
            seq += 1
            yield row


def read_records(path: Path, *, chain: str | None = None) -> list[dict]:
    # Materializing consumers remain bounded; journal accounting uses streaming.
    return list(iter_records(path, chain=chain, max_bytes=128_000_000))


def build_ledger_labels(journal_path: Path, fills_path: Path) -> dict:
    """Return mature labels and explicit rejections; no candle/PnL fallback."""
    rejected: Counter = Counter()
    try:
        journal = []
        journal_tip = None
        retained_bytes = 0
        relevant = {"order_intent", "ml_accounting_checkpoint", "ml_funding_coverage", "funding_applied"}
        for record in iter_records(journal_path, chain="journal"):
            journal_tip = record["hash"]
            if record["kind"] in relevant:
                retained_bytes += len(json.dumps(record))
                if retained_bytes > 64_000_000:
                    raise ValueError("accounting_record_budget_exceeded")
                journal.append(record)
        fills = read_records(fills_path, chain="fills")
        orders: dict[str, dict] = {}
        order_times: dict[str, datetime] = {}
        checkpoints, coverage, funding = [], [], []
        for record in journal:
            p = record["payload"]
            if record["kind"] == "order_intent":
                coid = p["client_order_id"]
                if coid in orders and digest(orders[coid]) != digest(p):
                    raise ValueError("conflicting_order_identity")
                orders[coid] = p
                order_times.setdefault(coid, timestamp(record["ts"]))
            elif record["kind"] == "ml_accounting_checkpoint":
                checkpoints.append(record)
            elif record["kind"] == "ml_funding_coverage":
                coverage.append(record)
            elif record["kind"] == "funding_applied":
                funding.append(record)
        labels: list[dict] = []
        checkpoints = [
            c
            for c in checkpoints
            if type(c["payload"].get("fill_count")) is int
            and 0 < c["payload"]["fill_count"] <= len(fills)
            and fills[c["payload"]["fill_count"] - 1]["hash"] == c["payload"].get("fill_tip")
        ]
        active: list[dict] = []
        position = Decimal(0)
        seen: dict[tuple, str] = {}
        previous_time: datetime | None = None
        for fill in fills:
            ts = timestamp(fill["executed_at"])
            if timestamp(fill.get("recorded_at", fill["ts"])) < ts:
                raise ValueError("fill_record_precedes_execution")
            if previous_time and ts < previous_time:
                raise ValueError("nonmonotonic_fill_time")
            previous_time = ts
            key = (fill["client_order_id"], fill["exchange_seq"])
            semantic = digest(
                {k: v for k, v in fill.items() if k not in {"seq", "prev_hash", "hash"}}
            )
            if key in seen:
                if seen[key] != semantic:
                    raise ValueError("conflicting_fill")
                rejected["duplicate_fill"] += 1
                continue
            seen[key] = semantic
            qty = money(fill["quantity"])
            if qty <= 0 or money(fill["price"]) <= 0 or fill["side"] not in {"buy", "sell"}:
                raise ValueError("invalid_fill_geometry")
            signed = qty if fill["side"] == "buy" else -qty
            # No guessing allocation across owners, symbols, reversals or pyramids.
            if active and (
                fill["symbol"] != active[0]["symbol"]
                or position * signed > 0
                and fill["client_order_id"] != active[0]["client_order_id"]
            ):
                raise ValueError("overlapping_position_owners")
            if position and position * (position + signed) < 0:
                raise ValueError("position_reversal_unallocated")
            active.append(fill)
            position += signed
            if position == 0:
                try:
                    labels.append(
                        _finalize(active, orders, order_times, checkpoints, coverage, funding)
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    rejected[str(exc)] += 1
                active = []
        if active:
            rejected["position_still_open"] += 1
        # One ARM may not produce two separately booked labels.
        counts = Counter(x["decision_id"] for x in labels)
        rejected["decision_reused_across_episodes"] += sum(n for n in counts.values() if n > 1)
        labels = [x for x in labels if counts[x["decision_id"]] == 1]
        return {
            "schema": "ml_ledger_labels_v1",
            "state": "VERIFIED",
            "labels": labels,
            "rejections": {k: v for k, v in rejected.items() if v},
            "source_hash": digest(
                [journal_tip, fills[-1]["hash"] if fills else None]
            ),
            "can_trade": False,
            "can_promote": False,
        }
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return {
            "schema": "ml_ledger_labels_v1",
            "state": "BLOCKED",
            "labels": [],
            "rejections": {str(exc): 1},
            "can_trade": False,
            "can_promote": False,
        }


def _finalize(
    fills: list[dict],
    orders: dict,
    order_times: dict,
    checkpoints: list,
    coverage: list,
    funding: list,
) -> dict:
    first, last = fills[0], fills[-1]
    order = orders[first["client_order_id"]]
    evidence = order["execution_evidence"]
    envelope = DecisionEnvelope.from_dict(evidence["arm_envelope"])
    if evidence.get("path_id") != "kernel_v1" or order.get("decision_id") != envelope.decision_id:
        raise ValueError("entry_without_kernel_identity")
    if any(
        f.get("mode") != "paper"
        or f.get("quantity_unit") != "base"
        or f["symbol"] != envelope.symbol
        or f["strategy_id"] != envelope.strategy_id
        or f["venue"] != first["venue"]
        for f in fills
    ):
        raise ValueError("unsupported_or_mixed_book")
    if first["side"] != ("buy" if envelope.side == "long" else "sell"):
        raise ValueError("entry_side_mismatch")
    opened, closed = timestamp(first["executed_at"]), timestamp(last["executed_at"])
    if opened < envelope.permission_snapshot.decision_bar.close_time:
        raise ValueError("fill_before_decision")
    cost = evidence["cost_decision"]
    if not cost.get("cost_profile_id") or not cost.get("cost_config_sha256"):
        raise ValueError("cost_contract_unbound")
    if cost.get("approved") is not True or evidence.get("decision_id") != envelope.decision_id:
        raise ValueError("cost_approval_or_identity_missing")
    ids = {f["client_order_id"] for f in fills}
    for coid in ids:
        intent = orders[coid]["intent"]
        if order_times[coid] > min(
            timestamp(f["executed_at"]) for f in fills if f["client_order_id"] == coid
        ):
            raise ValueError("order_proof_recorded_after_fill")
        if intent["symbol"] != envelope.symbol or bool(intent.get("reduce_only")) != (
            coid != first["client_order_id"]
        ):
            raise ValueError("exit_not_reduce_only")
    valid_checks = [
        c
        for c in checkpoints
        if c["payload"].get("fill_count", 0) > last["seq"]
        and c["payload"].get("clean") is True
        and c["payload"].get("recovery_degraded") is False
        and not c["payload"].get("open_positions")
        and not c["payload"].get("unresolved_orders")
        and timestamp(c["ts"]) >= closed
    ]
    if not valid_checks:
        raise ValueError("flat_accounting_checkpoint_required")
    checkpoint = valid_checks[0]
    for coid in ids:
        reported = checkpoint["payload"]["orders"][coid]
        own = [f for f in fills if f["client_order_id"] == coid]
        if (
            reported["state"] not in {"filled", "cancelled"}
            or abs(money(reported["quantity"]) - sum(money(f["quantity"]) for f in own))
            > Decimal("1e-10")
            or abs(money(reported["fees_usd"]) - sum(money(f["fee_usd"]) for f in own))
            > Decimal("1e-8")
        ):
            raise ValueError("fill_quantity_or_fee_reconciliation_failed")
    receipts = [
        r
        for r in coverage
        if r["payload"].get("symbol") == envelope.symbol
        and r["payload"].get("complete") is True
        and r["payload"].get("source_sha256")
        and timestamp(r["payload"]["start"]) <= opened
        and timestamp(r["payload"]["end"]) >= closed
    ]
    if not receipts:
        raise ValueError("settled_funding_coverage_required")
    receipt = receipts[-1]
    signatures = {
        digest(
            sorted(
                (str(e["event_id"]), timestamp(e["ts"]).isoformat(), str(money(e["rate"])))
                for e in r["payload"]["events"]
                if opened <= timestamp(e["ts"]) <= closed
            )
        )
        for r in receipts
    }
    if len(signatures) != 1:
        raise ValueError("conflicting_funding_coverage")
    source_hash = str(receipt["payload"]["source_sha256"])
    if (
        len(source_hash) != 64
        or any(c not in "0123456789abcdef" for c in source_hash)
        or timestamp(receipt["ts"]) < timestamp(receipt["payload"]["end"])
    ):
        raise ValueError("invalid_funding_coverage_receipt")
    expected = {
        str(e["event_id"])
        for e in receipt["payload"]["events"]
        if opened <= timestamp(e["ts"]) <= closed
    }
    expected_events = {str(e["event_id"]): e for e in receipt["payload"]["events"]}
    if len(expected) != sum(
        opened <= timestamp(e["ts"]) <= closed for e in receipt["payload"]["events"]
    ):
        raise ValueError("duplicate_funding_coverage_event")
    payments: dict[str, dict] = {}
    payment_records: dict[str, dict] = {}
    for r in funding:
        p = r["payload"]
        if p.get("symbol") != envelope.symbol or p.get("book") != "paper":
            continue
        event_ts = datetime.fromtimestamp(p["funding_ts_ms"] / 1000, UTC)
        applied_at = timestamp(p["applied_at"]) if p.get("applied_at") else timestamp(r["ts"])
        if opened <= applied_at <= closed and not opened <= event_ts <= closed:
            raise ValueError("funding_charged_to_wrong_episode")
        if opened <= event_ts <= closed:
            eid = str(p["funding_event_id"])
            expected_event = expected_events.get(eid)
            if (
                expected_event is None
                or timestamp(expected_event["ts"]) != event_ts
                or money(expected_event["rate"]) != money(p["funding_rate"])
            ):
                raise ValueError("funding_rate_not_verified_by_coverage")
            if event_ts in {opened, closed}:
                raise ValueError("funding_fill_order_ambiguous")
            if eid in payments and digest(payments[eid]) != digest(p):
                raise ValueError("conflicting_funding_payment")
            expected_payment = (
                money(p["notional_usd"])
                * money(p["funding_rate"])
                * (1 if envelope.side == "long" else -1)
            )
            if p["side"] != envelope.side or abs(
                expected_payment - money(p["funding_cost_usd"])
            ) > Decimal("0.000001"):
                raise ValueError("funding_cashflow_mismatch")
            payments[eid] = p
            payment_records[eid] = r
    if set(payments) != expected:
        raise ValueError("funding_payments_do_not_match_coverage")
    gross = sum(money(f["realized_pnl_usd"]) for f in fills)
    cash_move = sum(
        money(f["quantity"]) * money(f["price"]) * (1 if f["side"] == "sell" else -1) for f in fills
    )
    if abs(gross - cash_move) > Decimal("0.000001"):
        raise ValueError("realized_cashflow_mismatch")
    fees = sum(money(f["fee_usd"]) for f in fills)
    fund = sum((money(p["funding_cost_usd"]) for p in payments.values()), Decimal(0))
    notional = sum(
        money(f["quantity"]) * money(f["price"])
        for f in fills
        if f["client_order_id"] == first["client_order_id"]
    )
    net = gross - fees - fund
    label = {
        "decision_id": envelope.decision_id,
        "arm_envelope": envelope.as_dict(),
        "mode": "paper",
        "strategy_id": envelope.strategy_id,
        "exchange": first["venue"],
        "symbol": envelope.symbol,
        "timeframe": envelope.timeframe,
        "entry_clock": envelope.entry_clock,
        "cost_profile_id": cost["cost_profile_id"],
        "cost_config_sha256": cost["cost_config_sha256"],
        "label_contract": "reconciled_paper_net_positive_v1",
        "entry_at": opened.isoformat(),
        "exit_at": closed.isoformat(),
        "available_at": max(
            [
                timestamp(checkpoint["ts"]),
                timestamp(receipt["ts"]),
                *(timestamp(r["ts"]) for r in payment_records.values()),
            ]
        ).isoformat(),
        "gross_usd": float(gross),
        "fees_usd": float(fees),
        "funding_usd": float(fund),
        "net_usd": float(net),
        "net_bps": float(net / notional * 10000),
        "target": int(net > 0),
        "fill_hashes": [f["hash"] for f in fills],
        "checkpoint_hash": checkpoint["hash"],
        "funding_coverage_hash": receipt["hash"],
        "performance_eligible": False,
    }
    label["funding_payment_hashes"] = [
        payment_records[key]["hash"] for key in sorted(payment_records)
    ]
    return {**label, "label_hash": digest(label)}
