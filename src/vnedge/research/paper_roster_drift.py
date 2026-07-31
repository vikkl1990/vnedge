"""Paper roster drift reconciler.

The paper lane governor proposes a bounded paper roster. The runtime can still
show additional paper lanes when observation probes, old manifests, or demotion
queues stay online. This module makes that drift explicit.

Read-only by design: it cannot start, stop, promote, demote, or trade a lane.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_GOVERNOR = DEFAULT_RESEARCH_DIR / "paper_lane_governor_latest.json"
DEFAULT_SCANNER = DEFAULT_RESEARCH_DIR / "realtime_scanner_latest.json"
DEFAULT_ACTIVATION = DEFAULT_RESEARCH_DIR / "paper_lane_activation_latest.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "paper_roster_drift_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "paper_roster_drift_feed.jsonl"

STATE_EXPECTED_RUNNING = "EXPECTED_RUNNING"
STATE_EXPECTED_MISSING = "EXPECTED_MISSING"
STATE_EXTRA_RUNNING_PAPER = "EXTRA_RUNNING_PAPER"
STATE_DEMOTION_STILL_RUNNING = "DEMOTION_STILL_RUNNING"
STATE_PROBATION_STILL_RUNNING = "PROBATION_STILL_RUNNING"
STATE_REPAIR_STILL_RUNNING = "REPAIR_STILL_RUNNING"


@dataclass(frozen=True)
class PaperRosterDriftConfig:
    max_rows: int = 180

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_paper_roster_drift(
    *,
    governor: Mapping[str, Any] | None = None,
    scanner: Mapping[str, Any] | None = None,
    activation: Mapping[str, Any] | None = None,
    governor_path: Path | str = DEFAULT_GOVERNOR,
    scanner_path: Path | str = DEFAULT_SCANNER,
    activation_path: Path | str = DEFAULT_ACTIVATION,
    config: PaperRosterDriftConfig = PaperRosterDriftConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only roster drift payload."""

    now = now or datetime.now(UTC)
    governor_path = Path(governor_path)
    scanner_path = Path(scanner_path)
    activation_path = Path(activation_path)
    governor_payload = (
        dict(governor)
        if isinstance(governor, Mapping)
        else _read_json_payload(governor_path, {"proposed_roster": {}, "rows": []})
    )
    scanner_payload = (
        dict(scanner)
        if isinstance(scanner, Mapping)
        else _read_json_payload(scanner_path, {"rows": [], "summary": {}})
    )
    activation_payload = (
        dict(activation)
        if isinstance(activation, Mapping)
        else _read_json_payload(activation_path, {"rows": [], "summary": {}})
    )

    proposed = (
        governor_payload.get("proposed_roster")
        if isinstance(governor_payload.get("proposed_roster"), Mapping)
        else {}
    )
    expected = _refs(proposed.get("paper_lanes"), source="governor_paper_roster")
    demotion = _refs(proposed.get("demote_to_shadow"), source="governor_demotion")
    probation = _refs(proposed.get("probation_shadow_watch"), source="governor_probation")
    repair = _refs(proposed.get("repair_first"), source="governor_repair")
    actual = _actual_paper_refs(scanner_payload, activation_payload)

    actual_index = _index_refs(actual)
    expected_index = _index_refs(expected)
    demotion_index = _index_refs(demotion)
    probation_index = _index_refs(probation)
    repair_index = _index_refs(repair)

    rows: list[dict[str, Any]] = []
    matched_actual_keys: set[str] = set()
    for ref in expected:
        match = _find_match(ref, actual_index)
        if match is not None:
            matched_actual_keys.update(match["_keys"])
            rows.append(_row(STATE_EXPECTED_RUNNING, ref, actual=match))
        else:
            rows.append(_row(STATE_EXPECTED_MISSING, ref))

    for ref in actual:
        if ref["_keys"] & matched_actual_keys or _find_match(ref, expected_index):
            continue
        status = STATE_EXTRA_RUNNING_PAPER
        if _find_match(ref, demotion_index):
            status = STATE_DEMOTION_STILL_RUNNING
        elif _find_match(ref, probation_index):
            status = STATE_PROBATION_STILL_RUNNING
        elif _find_match(ref, repair_index):
            status = STATE_REPAIR_STILL_RUNNING
        rows.append(_row(status, ref))

    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows, expected=expected, actual=actual)
    return {
        "generated_at": now.isoformat(),
        "report_id": "paper_roster_drift_v1",
        "mode": "read_only_paper_roster_drift",
        "inputs": {
            "governor_path": str(governor_path),
            "scanner_path": str(scanner_path),
            "activation_path": str(activation_path),
        },
        "config": config.to_dict(),
        "summary": summary,
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "can_trade": False,
            "can_promote": False,
            "can_demote": False,
            "purpose": "detect paper lanes that do not match the governor proposal",
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_roster_drift(
    payload: Mapping[str, Any], out: Path, feed: Path | None = None
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def render_report(payload: Mapping[str, Any], *, limit: int = 40) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Paper roster drift ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('expected_paper_lanes', 0)} expected, "
            f"{summary.get('actual_paper_lanes', 0)} actual, "
            f"{summary.get('extra_paper_lanes', 0)} extra, "
            f"{summary.get('missing_paper_lanes', 0)} missing, "
            f"{summary.get('demotion_queue_running', 0)} demotion-running"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('drift_state', ''):<26} "
            f"{row.get('lane_id', ''):<42} "
            f"{row.get('exchange', ''):<12} {row.get('symbol', ''):<14} "
            f"{row.get('timeframe', ''):<4} {row.get('strategy_id', ''):<28} "
            f"{row.get('next_action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false can_demote=false")
    return "\n".join(lines)


def _actual_paper_refs(
    scanner_payload: Mapping[str, Any],
    activation_payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in scanner_payload.get("rows", []) or []:
        if not isinstance(row, Mapping) or not _is_paper_runtime(row):
            continue
        ref = _ref(row, source="realtime_scanner")
        if not ref["_primary_key"] or ref["_primary_key"] in seen:
            continue
        out.append(ref)
        seen.add(ref["_primary_key"])
    for row in activation_payload.get("rows", []) or []:
        if not isinstance(row, Mapping) or not _is_activation_running(row):
            continue
        runtime = row.get("runtime") if isinstance(row.get("runtime"), Mapping) else {}
        lane_ids = [
            str(item)
            for item in runtime.get("desired_lane_ids", []) or []
            if str(item).strip()
        ] or [str(row.get("trial_id") or row.get("lane_id") or "")]
        for lane_id in lane_ids:
            ref = _ref({**dict(row), "lane_id": lane_id}, source="paper_activation")
            if not ref["_primary_key"] or ref["_primary_key"] in seen:
                continue
            out.append(ref)
            seen.add(ref["_primary_key"])
    return out


def _refs(rows: Any, *, source: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if isinstance(row, Mapping):
            ref = _ref(row, source=source)
            if ref["_primary_key"]:
                out.append(ref)
    return out


def _ref(row: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    lane_id = str(row.get("lane_id") or row.get("trial_id") or "").strip()
    strategy_id = str(row.get("strategy_id") or row.get("family") or "").strip()
    exchange = str(row.get("exchange") or row.get("venue") or "").strip()
    symbol = str(row.get("symbol") or "").strip()
    timeframe = str(row.get("timeframe") or "").strip()
    lane_key = str(row.get("lane_key") or "").strip().lower()
    signature = _signature(strategy_id, exchange, symbol, timeframe)
    keys = {key for key in (lane_id, lane_key, signature) if key}
    return {
        "lane_id": lane_id,
        "lane_key": lane_key,
        "signature": signature,
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "mode": row.get("mode"),
        "state": row.get("state") or row.get("activation_state") or row.get("governor_bucket"),
        "source": source,
        "_primary_key": lane_id or lane_key or signature,
        "_keys": keys,
    }


def _index_refs(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for key in row["_keys"]:
            out.setdefault(key, []).append(row)
    return out


def _find_match(
    ref: Mapping[str, Any], index: Mapping[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    for key in ref.get("_keys", set()):
        matches = index.get(str(key), [])
        if matches:
            return matches[0]
    return None


def _row(
    state: str,
    ref: Mapping[str, Any],
    *,
    actual: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = actual or ref
    return {
        "drift_state": state,
        "lane_id": source.get("lane_id") or ref.get("lane_id"),
        "lane_key": source.get("lane_key") or ref.get("lane_key"),
        "signature": source.get("signature") or ref.get("signature"),
        "exchange": source.get("exchange") or ref.get("exchange"),
        "symbol": source.get("symbol") or ref.get("symbol"),
        "timeframe": source.get("timeframe") or ref.get("timeframe"),
        "strategy_id": source.get("strategy_id") or ref.get("strategy_id"),
        "expected_source": ref.get("source"),
        "actual_source": actual.get("source") if actual else None,
        "runtime_state": actual.get("state") if actual else None,
        "next_action": _next_action(state),
        "can_trade": False,
        "can_promote": False,
    }


def _summary(
    rows: list[dict[str, Any]],
    *,
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> dict[str, Any]:
    missing = sum(1 for row in rows if row.get("drift_state") == STATE_EXPECTED_MISSING)
    non_expected = sum(
        1
        for row in rows
        if row.get("drift_state")
        in {
            STATE_EXTRA_RUNNING_PAPER,
            STATE_DEMOTION_STILL_RUNNING,
            STATE_PROBATION_STILL_RUNNING,
            STATE_REPAIR_STILL_RUNNING,
        }
    )
    demotion = sum(1 for row in rows if row.get("drift_state") == STATE_DEMOTION_STILL_RUNNING)
    probation = sum(1 for row in rows if row.get("drift_state") == STATE_PROBATION_STILL_RUNNING)
    repair = sum(1 for row in rows if row.get("drift_state") == STATE_REPAIR_STILL_RUNNING)
    expected_running = sum(1 for row in rows if row.get("drift_state") == STATE_EXPECTED_RUNNING)
    return {
        "expected_paper_lanes": len(expected),
        "actual_paper_lanes": len(actual),
        "expected_running": expected_running,
        "missing_paper_lanes": missing,
        "extra_paper_lanes": non_expected,
        "demotion_queue_running": demotion,
        "probation_queue_running": probation,
        "repair_queue_running": repair,
        "drift_lanes": missing + non_expected,
        "drift_detected": bool(missing or non_expected),
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    extra = int(summary.get("extra_paper_lanes") or 0)
    missing = int(summary.get("missing_paper_lanes") or 0)
    demotion = int(summary.get("demotion_queue_running") or 0)
    if demotion:
        return (
            f"{demotion} demotion-queue lane(s) still appear as paper; move them "
            "back to shadow before trusting paper PnL."
        )
    if extra:
        return f"{extra} paper lane(s) are outside the governor roster; reconcile runtime roster."
    if missing:
        return f"{missing} governor paper lane(s) are missing from runtime evidence."
    return "Paper runtime matches the governor proposal."


def _next_action(state: str) -> str:
    if state == STATE_EXPECTED_RUNNING:
        return "keep observing paper lane"
    if state == STATE_EXPECTED_MISSING:
        return "start or repair the approved paper route"
    if state == STATE_DEMOTION_STILL_RUNNING:
        return "demote this paper lane back to shadow"
    if state == STATE_PROBATION_STILL_RUNNING:
        return "hold out of active paper roster until probation clears"
    if state == STATE_REPAIR_STILL_RUNNING:
        return "repair route/cadence/ledger before spending paper cycles"
    return "review why this lane is paper outside the governor roster"


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, str]:
    priority = {
        STATE_DEMOTION_STILL_RUNNING: 0,
        STATE_EXTRA_RUNNING_PAPER: 1,
        STATE_EXPECTED_MISSING: 2,
        STATE_PROBATION_STILL_RUNNING: 3,
        STATE_REPAIR_STILL_RUNNING: 4,
        STATE_EXPECTED_RUNNING: 5,
    }.get(str(row.get("drift_state") or ""), 9)
    return (priority, str(row.get("lane_id") or row.get("signature") or ""))


def _is_paper_runtime(row: Mapping[str, Any]) -> bool:
    mode = str(row.get("mode") or "").lower()
    lane = str(row.get("lane_id") or "").lower()
    if "paper" in mode or "_paper" in lane or lane.startswith("papertrial_"):
        return True
    funnel = row.get("funnel") if isinstance(row.get("funnel"), Mapping) else {}
    return int(funnel.get("paper_order_intents") or 0) > 0


def _is_activation_running(row: Mapping[str, Any]) -> bool:
    return str(row.get("activation_state") or "") in {
        "PAPER_RUNNING",
        "PAPER_ONLINE_WAITING",
    }


def _signature(strategy_id: str, exchange: str, symbol: str, timeframe: str) -> str:
    parts = [
        strategy_id.strip().lower(),
        exchange.strip().lower(),
        _norm_symbol(symbol),
        timeframe.strip().lower(),
    ]
    return "|".join(parts) if any(parts) else ""


def _norm_symbol(symbol: str) -> str:
    return str(symbol or "").split(":", 1)[0].strip().lower()


def _read_json_payload(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--governor", type=Path, default=DEFAULT_GOVERNOR)
    parser.add_argument("--scanner", type=Path, default=DEFAULT_SCANNER)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--interval-seconds", type=_positive_float, default=60.0)
    parser.add_argument("--max-rows", type=_positive_int, default=180)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = PaperRosterDriftConfig(max_rows=args.max_rows)
    while True:
        payload = build_paper_roster_drift(
            governor_path=args.governor,
            scanner_path=args.scanner,
            activation_path=args.activation,
            config=config,
        )
        publish_paper_roster_drift(payload, args.out, args.feed)
        if args.print:
            print(render_report(payload))
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
