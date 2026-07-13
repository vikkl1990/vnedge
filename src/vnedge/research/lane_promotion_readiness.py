"""Lane promotion readiness: one operator-facing truth table.

This module answers the question the raw dashboard counters cannot answer by
themselves: which lanes are merely configured, which are actually firing, and
which have enough live shadow evidence to deserve a paper-trial review?

It is read-only and conservative. A "paper review" label is not promotion, and
live readiness is always false here unless a future paper/live evidence reader
is explicitly added. The live ladder remains the authority.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from vnedge.research.shadow_manifest import load_shadow_manifest
from vnedge.research.shadow_perf_reader import (
    DEFAULT_JOURNAL_DIR,
    index_shadow_perf,
    read_shadow_perf,
    shadow_perf_key,
)

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "lane_promotion_readiness_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "lane_promotion_readiness_feed.jsonl"

STATUS_PAPER_REVIEW_READY = "PAPER_REVIEW_READY"
STATUS_SHADOW_COLLECTING = "SHADOW_COLLECTING"
STATUS_SHADOW_NOT_FIRING = "SHADOW_NOT_FIRING"
STATUS_SHADOW_NEGATIVE = "SHADOW_NEGATIVE"
STATUS_SHADOW_PF_TOO_LOW = "SHADOW_PF_TOO_LOW"
STATUS_REPLAY_NEEDS_ADAPTER = "REPLAY_POSITIVE_NEEDS_SHADOW_ADAPTER"
STATUS_BLOCKED = "BLOCKED"
STATUS_PAPER_ACTIVE = "PAPER_ACTIVE"
STATUS_PAPER_WAITING = "PAPER_WAITING_FOR_SIGNAL"


@dataclass(frozen=True)
class ReadinessConfig:
    min_shadow_trades: int = 10
    min_shadow_span_days: float = 7.0
    min_shadow_profit_factor: float = 1.25
    min_shadow_net_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_lane_promotion_readiness(
    *,
    research_dir: Path | str = DEFAULT_RESEARCH_DIR,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    config: ReadinessConfig = ReadinessConfig(),
) -> dict[str, Any]:
    """Build a JSON-serializable readiness report from persisted artifacts."""
    research_dir = Path(research_dir)
    manifest = load_shadow_manifest(research_dir)
    shadow_perf = read_shadow_perf(journal_dir)
    shadow_index = index_shadow_perf(shadow_perf)

    rows: list[dict[str, Any]] = []
    rows.extend(
        _manifest_lane_row(lane, shadow_index, config)
        for lane in manifest.get("lanes", []) or []
        if isinstance(lane, Mapping)
    )
    rows.extend(
        _shadow_trial_row(trial)
        for trial in manifest.get("shadow_trials", []) or []
        if isinstance(trial, Mapping)
    )
    rows.extend(
        _blocked_row(blocked)
        for blocked in manifest.get("blocked", []) or []
        if isinstance(blocked, Mapping)
    )
    rows.extend(_paper_trial_rows(Path(journal_dir)))
    rows.sort(key=_row_sort_key)

    summary = _summary(rows)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": {
            "status": "read_only_readiness",
            "can_trade": False,
            "can_promote": False,
            "paper_review_is_not_promotion": True,
            "live_ready_requires_completed_paper_trial_and_live_safety_gates": True,
        },
        "config": config.to_dict(),
        "inputs": {
            "research_dir": str(research_dir),
            "journal_dir": str(journal_dir),
            "shadow_manifest": str(research_dir / "shadow_lanes.json"),
        },
        "summary": summary,
        "rows": rows,
        "shadow_perf": {
            "available": bool(shadow_perf.get("available")),
            "journals_read": int(shadow_perf.get("journals_read") or 0),
        },
        "operator_answer": _operator_answer(summary),
        "can_trade": False,
        "can_promote": False,
    }


def publish_readiness(payload: Mapping[str, Any], out: Path, feed: Path | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def render_report(payload: Mapping[str, Any], *, limit: int = 30) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Lane promotion readiness ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_rows', 0)} rows, "
            f"{summary.get('paper_active', 0)} paper-active, "
            f"{summary.get('shadow_firing', 0)} firing, "
            f"{summary.get('paper_review_ready', 0)} paper-review candidates, "
            f"{summary.get('live_ready', 0)} live-ready"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        evidence = row.get("evidence", {})
        trades = evidence.get("virtual_trades", evidence.get("paper_order_intents", 0))
        net = evidence.get("net_usd", evidence.get("realized_pnl_usd"))
        lines.append(
            f"  {row.get('status', 'UNKNOWN'):<38} "
            f"{row.get('exchange', '')} {row.get('symbol', '')} "
            f"{row.get('strategy_id') or row.get('family') or row.get('source', '')} "
            f"trades={trades} "
            f"net={net}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _manifest_lane_row(
    lane: Mapping[str, Any],
    shadow_index: Mapping[str, dict],
    config: ReadinessConfig,
) -> dict[str, Any]:
    strategy = str(lane.get("strategy_id") or "")
    exchange = str(lane.get("exchange") or "")
    symbol = str(lane.get("symbol") or "")
    perf = shadow_index.get(shadow_perf_key(strategy, exchange, symbol))
    status, blockers = _shadow_status(perf, config)
    paper_review_ready = status == STATUS_PAPER_REVIEW_READY
    return {
        "row_type": "runtime_shadow_lane",
        "lane_id": lane.get("lane_id"),
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": str(lane.get("timeframe") or "1h"),
        "strategy_id": strategy,
        "mode": str(lane.get("mode") or "shadow"),
        "status": status,
        "paper_review_ready": paper_review_ready,
        "live_ready": False,
        "can_trade": False,
        "can_promote": False,
        "evidence": _shadow_evidence(perf),
        "blockers": blockers,
        "next_action": (
            "open human paper-trial approval review"
            if paper_review_ready
            else "keep shadow lane running until firing and positive after costs"
        ),
        "live_blockers": [
            "paper trial not completed",
            "pre-live checklist not cleared",
            "three live gates not open",
            "real execution adapter not mounted by this report",
        ],
    }


def _shadow_trial_row(trial: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "row_type": "filtered_replay_shadow_trial",
        "trial_id": trial.get("trial_id"),
        "candidate_id": trial.get("candidate_id"),
        "source": trial.get("source"),
        "family": trial.get("family"),
        "exchange": trial.get("exchange"),
        "symbol": trial.get("symbol"),
        "timeframe": trial.get("timeframe"),
        "mode": "shadow_trial",
        "status": STATUS_REPLAY_NEEDS_ADAPTER,
        "paper_review_ready": False,
        "live_ready": False,
        "can_trade": False,
        "can_promote": False,
        "evidence": {"filtered_replay": trial.get("replay", {})},
        "blockers": [
            "replay-positive event has no runtime shadow adapter",
            "no live shadow outcomes yet",
            "paper trial not approved",
        ],
        "next_action": trial.get("next_action")
        or "build runtime shadow adapter, then collect live shadow outcomes",
    }


def _blocked_row(blocked: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "row_type": "blocked_manifest_candidate",
        "exchange": blocked.get("exchange"),
        "symbol": blocked.get("symbol"),
        "strategy_id": blocked.get("strategy_id"),
        "mode": "blocked",
        "status": STATUS_BLOCKED,
        "paper_review_ready": False,
        "live_ready": False,
        "can_trade": False,
        "can_promote": False,
        "evidence": {"latest_judgment": blocked.get("latest_judgment")},
        "blockers": [str(blocked.get("reason") or "blocked by manifest")],
        "next_action": "resolve blocker with a fresh approved judgment or locked runtime params",
    }


def _paper_trial_rows(journal_dir: Path) -> list[dict[str, Any]]:
    """Summarize approved live-paper lanes from runtime journals."""
    if not journal_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(journal_dir.glob("*.journal.jsonl")):
        if path.name.endswith("_shadow.journal.jsonl"):
            continue
        evidence = _paper_evidence(path)
        if evidence is None:
            continue
        status = (
            STATUS_PAPER_ACTIVE
            if int(evidence.get("paper_order_intents") or 0) > 0
            else STATUS_PAPER_WAITING
        )
        rows.append({
            "row_type": "paper_trial_lane",
            "lane_id": path.name.removesuffix(".journal.jsonl"),
            "trial_id": path.name.removesuffix(".journal.jsonl"),
            "journal": str(path),
            "exchange": evidence.get("exchange") or "",
            "symbol": evidence.get("symbol") or "",
            "timeframe": evidence.get("timeframe") or "1h",
            "strategy_id": evidence.get("strategy_id") or "",
            "mode": "paper",
            "status": status,
            "paper_active": status == STATUS_PAPER_ACTIVE,
            "paper_review_ready": False,
            "live_ready": False,
            "can_trade": False,
            "can_promote": False,
            "evidence": evidence,
            "blockers": _paper_blockers(evidence),
            "next_action": (
                "keep approved paper trial running and judge only against its locked manifest"
                if status == STATUS_PAPER_ACTIVE
                else "keep approved paper trial online until the next valid signal"
            ),
            "live_blockers": [
                "paper trial verdict not complete",
                "pre-live checklist not cleared",
                "three live gates not open",
            ],
        })
    return rows


def _paper_evidence(path: Path) -> dict[str, Any] | None:
    counters = {
        "evals": 0,
        "live_signals": 0,
        "risk_decisions": 0,
        "paper_order_intents": 0,
        "paper_order_acknowledged": 0,
        "paper_exits": 0,
        "paper_reports": 0,
    }
    active_since_ts: str | None = None
    latest_eval_ts: str | None = None
    latest_bar_ts: str | None = None
    latest_signal_reason: str | None = None
    latest_order: dict[str, Any] | None = None
    latest_exit: dict[str, Any] | None = None
    latest_report: dict[str, Any] | None = None
    symbol = ""
    strategy_id = ""
    exchange = ""

    for record in _iter_jsonl(path):
        ts = str(record.get("ts") or "")
        if ts and active_since_ts is None:
            active_since_ts = ts
        kind = str(record.get("kind") or "")
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        if kind == "lane_eval":
            counters["evals"] += 1
            latest_eval_ts = ts or latest_eval_ts
            latest_bar_ts = str(payload.get("bar_ts") or latest_bar_ts or "")
            symbol = str(payload.get("symbol") or symbol)
            strategy_id = str(payload.get("strategy_id") or strategy_id)
            exchange = str(payload.get("exchange") or exchange)
            if bool(payload.get("fired")) and not bool(payload.get("backfill")):
                counters["live_signals"] += 1
                latest_signal_reason = str(payload.get("signal_reason") or "")
        elif kind == "risk_decision":
            counters["risk_decisions"] += 1
        elif kind == "order_intent":
            counters["paper_order_intents"] += 1
            latest_order = dict(payload)
            intent = payload.get("intent")
            if isinstance(intent, Mapping):
                symbol = str(intent.get("symbol") or symbol)
                strategy_id = str(intent.get("strategy_id") or strategy_id)
        elif kind == "order_acknowledged":
            counters["paper_order_acknowledged"] += 1
        elif kind == "live_paper_exit":
            counters["paper_exits"] += 1
            latest_exit = dict(payload)
        elif kind == "live_paper_report":
            counters["paper_reports"] += 1
            latest_report = dict(payload.get("report") or payload)
            symbol = str(latest_report.get("symbol") or symbol)
            strategy_id = str(latest_report.get("strategy_id") or strategy_id)

    if not any(counters.values()):
        return None
    if counters["evals"] == 0 and counters["paper_order_intents"] == 0 and latest_report is None:
        return None

    report = latest_report or {}
    return {
        **counters,
        "active_since_ts": active_since_ts,
        "latest_eval_ts": latest_eval_ts,
        "latest_bar_ts": latest_bar_ts or None,
        "latest_signal_reason": latest_signal_reason,
        "latest_order": _compact_paper_order(latest_order),
        "latest_exit": latest_exit,
        "latest_report": report,
        "symbol": symbol or report.get("symbol") or "",
        "strategy_id": strategy_id or report.get("strategy_id") or "",
        "exchange": exchange,
        "timeframe": "1h",
        "orders_submitted": report.get("orders_submitted"),
        "fills": report.get("fills"),
        "realized_pnl_usd": report.get("realized_pnl_usd"),
        "unrealized_pnl_usd": report.get("unrealized_pnl_usd"),
        "final_equity_usd": report.get("final_equity_usd"),
        "risk_rejects": report.get("risk_rejects"),
    }


def _compact_paper_order(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    intent = payload.get("intent")
    if not isinstance(intent, Mapping):
        return {"intent_key": payload.get("intent_key")}
    return {
        "intent_key": payload.get("intent_key"),
        "client_order_id": payload.get("client_order_id"),
        "symbol": intent.get("symbol"),
        "side": intent.get("side"),
        "quantity": intent.get("quantity"),
        "reduce_only": intent.get("reduce_only"),
        "strategy_id": intent.get("strategy_id"),
        "order_type": intent.get("order_type"),
    }


def _paper_blockers(evidence: Mapping[str, Any]) -> list[str]:
    blockers = [
        "paper status is visibility only, not promotion",
        "live promotion still requires completed paper verdict and live ladder gates",
    ]
    if int(evidence.get("paper_order_intents") or 0) == 0:
        blockers.append("no paper order intents yet; waiting for the next approved signal")
    return blockers


def _shadow_status(
    perf: Mapping[str, Any] | None,
    config: ReadinessConfig,
) -> tuple[str, list[str]]:
    if not perf:
        return STATUS_SHADOW_NOT_FIRING, [
            "no resolved shadow_outcome records for this lane",
            "cannot discuss paper/live until the lane fires and outcomes settle",
        ]
    trades = int(perf.get("virtual_trades") or 0)
    span_days = float(perf.get("span_days") or 0.0)
    net = float(perf.get("net_usd") or 0.0)
    profit_factor = _optional_float(perf.get("profit_factor"))
    blockers: list[str] = []
    if trades < config.min_shadow_trades:
        blockers.append(
            f"shadow trades too few: {trades} < {config.min_shadow_trades}"
        )
    if span_days < config.min_shadow_span_days:
        blockers.append(
            f"shadow span too short: {span_days:g}d < {config.min_shadow_span_days:g}d"
        )
    if net <= config.min_shadow_net_usd:
        blockers.append(f"shadow net not positive after costs: ${net:.2f}")
    if profit_factor is None:
        blockers.append("shadow profit factor missing/undefined")
    elif profit_factor < config.min_shadow_profit_factor:
        blockers.append(
            f"shadow profit factor too low: {profit_factor:.2f} "
            f"< {config.min_shadow_profit_factor:.2f}"
        )
    if not blockers:
        return STATUS_PAPER_REVIEW_READY, []
    if trades == 0:
        return STATUS_SHADOW_NOT_FIRING, blockers
    if net <= config.min_shadow_net_usd:
        return STATUS_SHADOW_NEGATIVE, blockers
    if profit_factor is not None and profit_factor < config.min_shadow_profit_factor:
        return STATUS_SHADOW_PF_TOO_LOW, blockers
    return STATUS_SHADOW_COLLECTING, blockers


def _shadow_evidence(perf: Mapping[str, Any] | None) -> dict[str, Any]:
    if not perf:
        return {
            "virtual_trades": 0,
            "wins": 0,
            "net_usd": 0.0,
            "profit_factor": None,
            "span_days": 0.0,
        }
    return {
        "virtual_trades": int(perf.get("virtual_trades") or 0),
        "wins": int(perf.get("wins") or 0),
        "win_rate_pct": perf.get("win_rate_pct"),
        "net_usd": perf.get("net_usd"),
        "profit_factor": perf.get("profit_factor"),
        "span_days": perf.get("span_days"),
        "last_resolution_ts": perf.get("last_resolution_ts"),
        "resolutions": perf.get("resolutions", {}),
        "source_journals": perf.get("source_journals", []),
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "UNKNOWN")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "total_rows": len(rows),
        "runtime_shadow_lanes": sum(
            1 for row in rows if row.get("row_type") == "runtime_shadow_lane"
        ),
        "filtered_replay_shadow_trials": sum(
            1 for row in rows if row.get("row_type") == "filtered_replay_shadow_trial"
        ),
        "blocked": statuses.get(STATUS_BLOCKED, 0),
        "paper_runtime_lanes": sum(
            1 for row in rows if row.get("row_type") == "paper_trial_lane"
        ),
        "paper_active": sum(1 for row in rows if row.get("paper_active")),
        "paper_order_intents": sum(
            int(row.get("evidence", {}).get("paper_order_intents") or 0)
            for row in rows
            if row.get("row_type") == "paper_trial_lane"
        ),
        "paper_exits": sum(
            int(row.get("evidence", {}).get("paper_exits") or 0)
            for row in rows
            if row.get("row_type") == "paper_trial_lane"
        ),
        "shadow_firing": sum(
            1
            for row in rows
            if row.get("row_type") == "runtime_shadow_lane"
            and int(row.get("evidence", {}).get("virtual_trades") or 0) > 0
        ),
        "shadow_not_firing": statuses.get(STATUS_SHADOW_NOT_FIRING, 0),
        "shadow_negative": statuses.get(STATUS_SHADOW_NEGATIVE, 0),
        "paper_review_ready": sum(1 for row in rows if row.get("paper_review_ready")),
        "live_ready": sum(1 for row in rows if row.get("live_ready")),
        "status_counts": statuses,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    paper = int(summary.get("paper_review_ready") or 0)
    live = int(summary.get("live_ready") or 0)
    firing = int(summary.get("shadow_firing") or 0)
    not_firing = int(summary.get("shadow_not_firing") or 0)
    trials = int(summary.get("filtered_replay_shadow_trials") or 0)
    paper_active = int(summary.get("paper_active") or 0)
    paper_orders = int(summary.get("paper_order_intents") or 0)
    paper_exits = int(summary.get("paper_exits") or 0)
    return (
        f"{paper_active} approved paper lane(s) active "
        f"({paper_orders} order intents, {paper_exits} exits), "
        f"{paper} lane(s) are paper-review ready, {live} lane(s) are live-ready, "
        f"{firing} runtime shadow lane(s) are firing, {not_firing} are not firing, "
        f"and {trials} replay-positive trial(s) still need runtime adapters."
    )


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, str, str, str]:
    rank = {
        STATUS_PAPER_ACTIVE: 0,
        STATUS_PAPER_WAITING: 1,
        STATUS_PAPER_REVIEW_READY: 2,
        STATUS_SHADOW_COLLECTING: 3,
        STATUS_SHADOW_PF_TOO_LOW: 4,
        STATUS_SHADOW_NEGATIVE: 5,
        STATUS_SHADOW_NOT_FIRING: 6,
        STATUS_REPLAY_NEEDS_ADAPTER: 7,
        STATUS_BLOCKED: 8,
    }.get(str(row.get("status") or ""), 9)
    return (
        rank,
        str(row.get("exchange") or ""),
        str(row.get("symbol") or ""),
        str(row.get("lane_id") or row.get("trial_id") or row.get("strategy_id") or ""),
    )


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open() as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    rows.append(record)
    except OSError:
        return []
    return rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish lane promotion readiness")
    parser.add_argument("--research-dir", default=str(DEFAULT_RESEARCH_DIR))
    parser.add_argument("--journal-dir", default=str(DEFAULT_JOURNAL_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--min-shadow-trades", type=int, default=10)
    parser.add_argument("--min-shadow-span-days", type=float, default=7.0)
    parser.add_argument("--min-shadow-profit-factor", type=float, default=1.25)
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args(argv)


def _run_once(args: argparse.Namespace) -> dict[str, Any]:
    payload = build_lane_promotion_readiness(
        research_dir=args.research_dir,
        journal_dir=args.journal_dir,
        config=ReadinessConfig(
            min_shadow_trades=args.min_shadow_trades,
            min_shadow_span_days=args.min_shadow_span_days,
            min_shadow_profit_factor=args.min_shadow_profit_factor,
        ),
    )
    if not args.no_publish:
        publish_readiness(payload, Path(args.out), Path(args.feed) if args.feed else None)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(render_report(payload, limit=args.limit))
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        _run_once(args)
        if args.interval_seconds <= 0:
            break
        time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
