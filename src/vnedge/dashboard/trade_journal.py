"""Read-only trade journal projection for the dashboard.

The decision journal remains the source of truth for decisions and order
state; the fill ledger remains the source of truth for executions. This module
builds an operator-friendly view across those append-only files: positions,
orders, fills, resolved trades, and event chronology.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class TradeJournalConfig:
    tail_bytes: int = 4_000_000
    max_rows: int = 200


def build_trade_journal(
    *,
    snapshot: dict | None,
    journal_dir: Path | str | None,
    history_path: Path | str | None = None,
    lane: str = "",
    since: str | None = None,
    limit: int = 200,
    config: TradeJournalConfig = TradeJournalConfig(),
) -> dict[str, Any]:
    """Build a dashboard trade journal from snapshot + append-only artifacts.

    ``lane`` filters to one lane id. Empty lane means fleet view: scan every
    lane journal/fill ledger and include the current primary snapshot's live
    positions/orders.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    root = Path(journal_dir) if journal_dir is not None else None
    lane = lane.strip()
    limit = max(1, min(int(limit), config.max_rows))
    since_dt = _parse_dt(since)

    fills = _fill_rows(root, lane=lane, since=since_dt, config=config)
    journal_rows = _journal_rows(root, lane=lane, since=since_dt, config=config)

    positions = _snapshot_positions(snapshot, lane)
    snapshot_orders = _snapshot_orders(snapshot, lane)
    snapshot_fills = _snapshot_fills(snapshot, lane, since_dt)
    if not fills:
        fills = snapshot_fills

    order_rows, journal_events, virtual_trades = _project_journals(journal_rows)
    order_rows = _merge_snapshot_orders(order_rows, snapshot_orders)

    closed_trades = _closed_actual_trades(fills) + virtual_trades
    events = _snapshot_events(snapshot, lane, since_dt) + journal_events

    fills = _sort_recent(fills)[:limit]
    order_rows = _sort_recent(order_rows)[:limit]
    closed_trades = _sort_recent(closed_trades)[:limit]
    events = _sort_recent(events)[:limit]

    actual_realized = sum(_float(row.get("realized_pnl_usd")) for row in fills)
    fees = sum(_float(row.get("fee_usd")) for row in fills)
    virtual_net = sum(_float(row.get("virtual_net_usd")) for row in closed_trades)
    lane_counts = _lane_counts(snapshot)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "lane": lane or "all",
        "summary": {
            "positions": len(positions),
            "orders": len(order_rows),
            "open_orders": sum(1 for row in order_rows if _is_open_order(row)),
            "fills": len(fills),
            "closed_trades": len(closed_trades),
            "events": len(events),
            "journals_scanned": _count_paths(root, ".journal.jsonl", lane),
            "fill_ledgers_scanned": _count_paths(root, ".fills.jsonl", lane),
            "actual_realized_pnl_usd": round(actual_realized, 6),
            "fees_usd": round(fees, 6),
            "virtual_net_usd": round(virtual_net, 6),
            "lane_position_counts": lane_counts,
            "history_lane": _primary_lane(history_path),
        },
        "positions": positions[:limit],
        "orders": order_rows,
        "fills": fills,
        "closed_trades": closed_trades,
        "events": events,
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "source": "snapshot + decision journals + hash-chained fill ledgers",
        },
        "can_trade": False,
        "can_promote": False,
    }


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return [line for line in lines if line.strip()]


def _iter_jsonl(path: Path, *, max_bytes: int) -> Iterable[dict[str, Any]]:
    for line in _tail_lines(path, max_bytes):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _paths(root: Path | None, suffix: str, lane: str) -> list[Path]:
    if root is None or not root.is_dir():
        return []
    if lane:
        candidate = root / f"{lane}{suffix}"
        return [candidate] if candidate.exists() else []
    return sorted(root.glob(f"*{suffix}"))


def _count_paths(root: Path | None, suffix: str, lane: str) -> int:
    return len(_paths(root, suffix, lane))


def _lane_from_path(path: Path, suffix: str) -> str:
    return path.name.removesuffix(suffix)


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_from_ms(value: object) -> str | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _record_ts(record: dict[str, Any], payload: dict[str, Any] | None = None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    for key in ("ts", "bar_ts", "resolved_bar_ts"):
        if record.get(key):
            return str(record[key])
        if payload.get(key):
            return str(payload[key])
    for key in ("exit_ts_ms", "entry_ts_ms", "impulse_ts_ms"):
        converted = _iso_from_ms(payload.get(key))
        if converted:
            return converted
    return ""


def _after_since(ts: object, since: datetime | None) -> bool:
    if since is None:
        return True
    parsed = _parse_dt(ts)
    return parsed is not None and parsed >= since


def _float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:
        return default
    return parsed


def _fill_rows(
    root: Path | None,
    *,
    lane: str,
    since: datetime | None,
    config: TradeJournalConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _paths(root, ".fills.jsonl", lane):
        lane_id = _lane_from_path(path, ".fills.jsonl")
        for raw in _iter_jsonl(path, max_bytes=config.tail_bytes):
            ts = _record_ts(raw)
            if not _after_since(ts, since):
                continue
            rows.append(
                {
                    "lane": lane_id,
                    "ts": ts,
                    "mode": raw.get("mode", ""),
                    "venue": raw.get("venue", ""),
                    "strategy_id": raw.get("strategy_id", ""),
                    "symbol": raw.get("symbol", ""),
                    "side": raw.get("side", ""),
                    "quantity": _float(raw.get("quantity")),
                    "price": _float(raw.get("price")),
                    "fee_usd": _float(raw.get("fee_usd")),
                    "realized_pnl_usd": _float(raw.get("realized_pnl_usd")),
                    "client_order_id": raw.get("client_order_id", ""),
                    "exchange_seq": raw.get("exchange_seq", raw.get("seq", "")),
                    "hash": raw.get("hash", ""),
                    "source": "fill_ledger",
                }
            )
    return rows


def _journal_rows(
    root: Path | None,
    *,
    lane: str,
    since: datetime | None,
    config: TradeJournalConfig,
) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for path in _paths(root, ".journal.jsonl", lane):
        lane_id = _lane_from_path(path, ".journal.jsonl")
        for raw in _iter_jsonl(path, max_bytes=config.tail_bytes):
            ts = _record_ts(raw, raw.get("payload") if isinstance(raw.get("payload"), dict) else {})
            if not _after_since(ts, since):
                continue
            rows.append((lane_id, raw))
    return rows


def _project_journals(
    journal_rows: list[tuple[str, dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    orders: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    virtual_trades: list[dict[str, Any]] = []
    for lane, record in journal_rows:
        kind = str(record.get("kind") or "")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        ts = _record_ts(record, payload)
        if kind in _ORDER_KINDS:
            _apply_order_event(orders, lane, ts, kind, payload)
        if kind in _EVENT_KINDS:
            events.append(
                {
                    "lane": lane,
                    "ts": ts,
                    "event": kind,
                    "detail": _event_detail(kind, payload),
                    "source": "decision_journal",
                }
            )
        if kind == "shadow_outcome":
            virtual_trades.append(_shadow_outcome_trade(lane, ts, payload))
        elif kind == "scalp_shadow_outcome":
            virtual_trades.append(_scalp_outcome_trade(lane, ts, payload))
    return list(orders.values()), events, virtual_trades


_ORDER_KINDS = {
    "risk_decision",
    "order_intent",
    "order_acknowledged",
    "order_rejected",
    "order_timeout_unknown",
    "order_refused",
    "order_cancel",
    "order_fill_sync",
    "order_resolved",
}

_EVENT_KINDS = _ORDER_KINDS | {
    "shadow_intent",
    "shadow_outcome",
    "scalp_shadow_intent",
    "scalp_shadow_outcome",
    "live_paper_exit",
    "paper_exit",
    "tick_stop_exit",
    "daily_report",
    "lane_eval",
    "executor_finished",
    "executor_scalper_risk_decision",
}


def _order_id(payload: dict[str, Any]) -> str:
    return str(payload.get("client_order_id") or payload.get("order_id") or "")


def _ensure_order(
    orders: dict[str, dict[str, Any]], lane: str, ts: str, coid: str
) -> dict[str, Any]:
    key = coid or f"{lane}|unknown|{len(orders)}"
    row = orders.setdefault(
        key,
        {
            "lane": lane,
            "ts": ts,
            "client_order_id": coid,
            "exchange_order_id": "",
            "symbol": "",
            "side": "",
            "order_type": "",
            "quantity": 0.0,
            "limit_price": None,
            "reduce_only": False,
            "strategy_id": "",
            "state": "observed",
            "last_event": "",
            "reason": "",
            "source": "decision_journal",
        },
    )
    row["ts"] = max(str(row.get("ts") or ""), ts)
    return row


def _apply_order_event(
    orders: dict[str, dict[str, Any]],
    lane: str,
    ts: str,
    kind: str,
    payload: dict[str, Any],
) -> None:
    coid = _order_id(payload)
    intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
    if not coid and isinstance(intent, dict):
        coid = str(payload.get("client_order_id") or "")
    row = _ensure_order(orders, lane, ts, coid)
    row["last_event"] = kind
    if intent:
        row.update(
            {
                "symbol": intent.get("symbol", row.get("symbol", "")),
                "side": intent.get("side", row.get("side", "")),
                "order_type": intent.get("order_type", row.get("order_type", "")),
                "quantity": _float(intent.get("quantity"), row.get("quantity", 0.0)),
                "limit_price": intent.get("limit_price", row.get("limit_price")),
                "reduce_only": bool(intent.get("reduce_only", row.get("reduce_only"))),
                "strategy_id": intent.get("strategy_id", row.get("strategy_id", "")),
            }
        )
    if kind == "risk_decision":
        row["state"] = "risk_approved" if payload.get("approved") else "risk_rejected"
        row["reason"] = ", ".join(payload.get("failed_checks") or [])
    elif kind == "order_intent":
        row["state"] = "intent_created"
    elif kind == "order_acknowledged":
        row["state"] = "acknowledged"
        row["exchange_order_id"] = str(payload.get("exchange_order_id") or "")
    elif kind == "order_rejected":
        row["state"] = "rejected"
        row["reason"] = str(payload.get("reason") or "")
    elif kind == "order_timeout_unknown":
        row["state"] = "timeout_unknown"
        row["reason"] = str(payload.get("detail") or "")
    elif kind == "order_refused":
        row["state"] = "refused"
        row["reason"] = str(payload.get("reason") or "")
    elif kind == "order_cancel":
        row["state"] = str(payload.get("venue_state") or "cancelled")
        row["filled_quantity"] = _float(payload.get("filled_quantity"))
        row["reason"] = str(payload.get("reason") or "")
    elif kind == "order_fill_sync":
        row["state"] = str(payload.get("state") or row.get("state"))
        row["filled_quantity"] = _float(payload.get("filled_quantity"))
        row["fees_paid"] = _float(payload.get("fees_paid"))
    elif kind == "order_resolved":
        row["state"] = "resolved"
        row["reason"] = str(payload.get("venue_state") or payload.get("reason") or "")


def _event_detail(kind: str, payload: dict[str, Any]) -> str:
    if kind == "lane_eval":
        fired = "fired" if payload.get("fired") else "waiting"
        reason = payload.get("signal_reason") or payload.get("skip_reason") or ""
        return f"{fired}: {reason}".strip(": ")
    if kind in {"shadow_outcome", "scalp_shadow_outcome"}:
        net = payload.get("virtual_net_usd", payload.get("taker_net_usd"))
        return (
            f"{payload.get('resolution', 'resolved')} {payload.get('side', '')} "
            f"virtual {net}"
        )
    if kind in {"shadow_intent", "scalp_shadow_intent"}:
        approved = "approved" if payload.get("approved") else "rejected"
        return f"{approved}: {payload.get('signal_reason', '')}".strip(": ")
    if kind == "risk_decision":
        return "approved" if payload.get("approved") else ", ".join(
            payload.get("failed_checks") or []
        )
    if kind == "order_intent":
        intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
        return f"{intent.get('side', '')} {intent.get('quantity', '')} {intent.get('symbol', '')}"
    if kind == "order_acknowledged":
        return f"exchange_order_id={payload.get('exchange_order_id', '')}"
    if kind in {"live_paper_exit", "paper_exit", "tick_stop_exit"}:
        return f"{payload.get('reason', kind)} {payload.get('state', '')}".strip()
    if kind == "daily_report":
        return str(payload.get("summary") or "")
    return ", ".join(f"{key}={value}" for key, value in list(payload.items())[:5])


def _closed_actual_trades(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fill in fills:
        realized = _float(fill.get("realized_pnl_usd"))
        if abs(realized) <= 1e-12:
            continue
        fee = _float(fill.get("fee_usd"))
        rows.append(
            {
                "lane": fill.get("lane", ""),
                "ts": fill.get("ts", ""),
                "kind": "actual_closing_fill",
                "symbol": fill.get("symbol", ""),
                "side": fill.get("side", ""),
                "quantity": fill.get("quantity", 0.0),
                "exit_price": fill.get("price", 0.0),
                "realized_pnl_usd": round(realized, 6),
                "fee_usd": round(fee, 6),
                "net_after_this_fill_fee_usd": round(realized - fee, 6),
                "client_order_id": fill.get("client_order_id", ""),
                "source": fill.get("source", "fill_ledger"),
            }
        )
    return rows


def _shadow_outcome_trade(lane: str, ts: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "lane": lane,
        "ts": ts,
        "kind": "shadow_outcome",
        "symbol": payload.get("symbol", ""),
        "side": payload.get("side", ""),
        "resolution": payload.get("resolution", ""),
        "entry_price": payload.get("entry_price"),
        "exit_price": payload.get("exit_price"),
        "virtual_net_usd": _float(payload.get("virtual_net_usd")),
        "fees_usd": _float(payload.get("fees_usd")),
        "intent_key": payload.get("intent_key", ""),
        "signal_reason": payload.get("signal_reason", ""),
        "source": "decision_journal",
    }


def _scalp_outcome_trade(lane: str, ts: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "lane": lane,
        "ts": ts,
        "kind": "scalp_shadow_outcome",
        "family": payload.get("family", ""),
        "side": payload.get("side", ""),
        "resolution": payload.get("resolution", ""),
        "entry_price": payload.get("entry_price", payload.get("taker_entry_price")),
        "exit_price": payload.get("exit_price", payload.get("taker_exit_price")),
        "virtual_net_usd": _float(payload.get("virtual_net_usd")),
        "taker_net_usd": payload.get("taker_net_usd"),
        "maker_net_usd": payload.get("maker_net_usd"),
        "taker_net_bps": payload.get("taker_net_bps"),
        "maker_net_bps": payload.get("maker_net_bps"),
        "maker_filled": payload.get("maker_filled"),
        "intent_key": payload.get("intent_key", ""),
        "source": "decision_journal",
    }


def _snapshot_positions(snapshot: dict[str, Any], lane: str) -> list[dict[str, Any]]:
    if lane and snapshot.get("lane_id") != lane:
        return []
    lane_id = str(snapshot.get("lane_id") or "primary")
    rows = []
    for pos in snapshot.get("positions") or []:
        if isinstance(pos, dict):
            rows.append({"lane": lane_id, **pos, "source": "snapshot"})
    return rows


def _snapshot_orders(snapshot: dict[str, Any], lane: str) -> list[dict[str, Any]]:
    if lane and snapshot.get("lane_id") != lane:
        return []
    lane_id = str(snapshot.get("lane_id") or "primary")
    rows = []
    for order in snapshot.get("open_orders") or []:
        if isinstance(order, dict):
            rows.append(
                {
                    "lane": lane_id,
                    "ts": snapshot.get("ts", ""),
                    "state": order.get("state", "open"),
                    "last_event": "snapshot_open_order",
                    "source": "snapshot",
                    **order,
                }
            )
    return rows


def _snapshot_fills(
    snapshot: dict[str, Any], lane: str, since: datetime | None
) -> list[dict[str, Any]]:
    if lane and snapshot.get("lane_id") != lane:
        return []
    lane_id = str(snapshot.get("lane_id") or "primary")
    ts = str(snapshot.get("ts") or "")
    if not _after_since(ts, since):
        return []
    rows = []
    for fill in snapshot.get("recent_fills") or []:
        if isinstance(fill, dict):
            rows.append({"lane": lane_id, "ts": ts, "source": "snapshot", **fill})
    return rows


def _snapshot_events(
    snapshot: dict[str, Any], lane: str, since: datetime | None
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for lane_id, log in _snapshot_trade_logs(snapshot, lane):
        for event in log:
            if not isinstance(event, dict):
                continue
            ts = str(event.get("ts") or "")
            if not _after_since(ts, since):
                continue
            events.append(
                {
                    "lane": lane_id,
                    "ts": ts,
                    "event": event.get("event", ""),
                    "detail": event.get("detail", ""),
                    "source": "snapshot_trade_log",
                }
            )
    return events


def _snapshot_trade_logs(
    snapshot: dict[str, Any], lane: str
) -> Iterable[tuple[str, list[dict[str, Any]]]]:
    if lane:
        if snapshot.get("lane_id") == lane:
            session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
            yield lane, [e for e in session.get("trade_log") or [] if isinstance(e, dict)]
        for entry in snapshot.get("lanes") or []:
            if isinstance(entry, dict) and entry.get("lane_id") == lane:
                yield lane, [e for e in entry.get("trade_log") or [] if isinstance(e, dict)]
        return
    session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
    if session.get("trade_log"):
        yield str(snapshot.get("lane_id") or "primary"), [
            e for e in session.get("trade_log") or [] if isinstance(e, dict)
        ]
    for entry in snapshot.get("lanes") or []:
        if isinstance(entry, dict):
            yield str(entry.get("lane_id") or "?"), [
                e for e in entry.get("trade_log") or [] if isinstance(e, dict)
            ]


def _merge_snapshot_orders(
    journal_orders: list[dict[str, Any]], snapshot_orders: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {str(row.get("client_order_id") or ""): row for row in journal_orders}
    anonymous: list[dict[str, Any]] = []
    for order in snapshot_orders:
        coid = str(order.get("client_order_id") or "")
        if coid and coid in by_id:
            by_id[coid].update(
                {
                    "state": order.get("state", by_id[coid].get("state")),
                    "source": f"{by_id[coid].get('source', 'journal')}+snapshot",
                    "snapshot_open": True,
                }
            )
        elif coid:
            by_id[coid] = order
        else:
            anonymous.append(order)
    return list(by_id.values()) + anonymous


def _is_open_order(row: dict[str, Any]) -> bool:
    state = str(row.get("state") or "").lower()
    return state in {
        "open",
        "acknowledged",
        "partially_filled",
        "timeout_unknown",
        "reconciling",
        "intent_created",
        "risk_approved",
    }


def _sort_recent(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get("ts") or ""), reverse=True)


def _lane_counts(snapshot: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for lane in snapshot.get("lanes") or []:
        if isinstance(lane, dict):
            out[str(lane.get("lane_id") or "?")] = int(_float(lane.get("positions")))
    return out


def _primary_lane(history_path: Path | str | None) -> str:
    if history_path is None:
        return "primary"
    name = Path(history_path).name
    return name.removesuffix(".equity.jsonl") if name.endswith(".equity.jsonl") else "primary"
