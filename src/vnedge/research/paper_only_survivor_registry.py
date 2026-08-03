"""Paper-only survivor registry.

Consumes the position-aware second-eye backtest grid and produces one operator
contract:

* PAPER_SURVIVOR - strict taker route clears sample/PF/avg-bps gates.
* PAPER_MAKER_PROBE - maker upper-bound clears gates, but taker does not.
* PAPER_QUARANTINE - the lane does not deserve more paper cycles yet.

This module is read-only. It never starts a lane, edits a manifest, promotes
capital, or submits orders. The multi-lane runner can consume its
``proposed_roster.paper_lanes`` through the existing paper-roster filter.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_GRID = DEFAULT_RESEARCH_DIR / "second_eye_grid.json"
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "paper_only_survivor_registry_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "paper_only_survivor_registry_feed.jsonl"

REPORT_ID = "paper_only_survivor_registry_v1"

STATE_PAPER_SURVIVOR = "PAPER_SURVIVOR"
STATE_PAPER_MAKER_PROBE = "PAPER_MAKER_PROBE"
STATE_PAPER_QUARANTINE = "PAPER_QUARANTINE"
STATE_BACKTEST_ERROR = "BACKTEST_ERROR"
STATE_NO_TRADE_SAMPLE = "NO_TRADE_SAMPLE"

ACTION_RUN_PAPER_ONLY = "RUN_PAPER_ONLY"
ACTION_RUN_PAPER_MAKER_PROBE = "RUN_PAPER_MAKER_PROBE"
ACTION_QUARANTINE_PAPER = "QUARANTINE_PAPER"
ACTION_REPAIR_BACKTEST = "REPAIR_BACKTEST"
ACTION_WAIT_FOR_SAMPLE = "WAIT_FOR_SAMPLE"


@dataclass(frozen=True)
class PaperOnlySurvivorRegistryConfig:
    min_trades: int = 20
    min_profit_factor: float = 1.5
    min_avg_net_bps: float = 25.0
    maker_probe_min_taker_bps: float = -10.0
    max_paper_lanes: int = 18
    max_rows: int = 240

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_paper_only_survivor_registry(
    *,
    grid: Mapping[str, Any] | None = None,
    grid_path: Path | str = DEFAULT_GRID,
    config: PaperOnlySurvivorRegistryConfig = PaperOnlySurvivorRegistryConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    grid_path = Path(grid_path)
    grid_payload = (
        dict(grid)
        if isinstance(grid, Mapping)
        else _read_json_payload(grid_path, {"rows": [], "pre_registry": {}})
    )
    rows = [
        _registry_row(row, config=config)
        for row in grid_payload.get("rows", []) or []
        if isinstance(row, Mapping)
    ]
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]
    summary = _summary(rows, config=config)
    return {
        "generated_at": now.isoformat(),
        "report_id": REPORT_ID,
        "mode": "read_only_paper_only_survivor_registry",
        "source_report": "second_eye_grid",
        "source_complete": bool(grid_payload.get("complete")),
        "source_progress": grid_payload.get("progress"),
        "source_total": grid_payload.get("total"),
        "source_pre_registry": grid_payload.get("pre_registry") or {},
        "inputs": {"grid_path": str(grid_path)},
        "config": config.to_dict(),
        "summary": summary,
        "proposed_roster": _proposed_roster(rows, config=config),
        "boards": _boards(rows),
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "policy": {
            "read_only": True,
            "paper_only": True,
            "shadow_roster_allowed": False,
            "can_trade": False,
            "can_promote": False,
            "human_review_required_before_runtime_roster_change": True,
            "maker_probe_requires_live_fill_quality": True,
        },
        "can_trade": False,
        "can_promote": False,
    }


def publish_paper_only_survivor_registry(
    payload: Mapping[str, Any], out: Path | str, feed: Path | str | None = DEFAULT_FEED
) -> None:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(out_path)
    if feed is not None:
        feed_path = Path(feed)
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        with feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str, sort_keys=True) + "\n")


def render_report(payload: Mapping[str, Any], *, limit: int = 40) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Paper-only survivor registry ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_cells', 0)} cells, "
            f"{summary.get('paper_survivors', 0)} strict survivors, "
            f"{summary.get('paper_maker_probes', 0)} maker probes, "
            f"{summary.get('paper_quarantine', 0)} quarantined"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('survivor_state', ''):<20} "
            f"{row.get('exchange', ''):<12} {row.get('symbol', ''):<14} "
            f"{row.get('timeframe', ''):<3} {row.get('strategy_id', ''):<32} "
            f"{row.get('selected_route', ''):<5} "
            f"n={row.get('samples', 0):>3} "
            f"PF={row.get('selected_profit_factor', 0):>5.2f} "
            f"bps={row.get('selected_avg_net_bps', 0):>7.2f} "
            f"{row.get('action', '')}"
        )
    lines.append("read-only: can_trade=false can_promote=false")
    return "\n".join(lines)


def _registry_row(
    source: Mapping[str, Any], *, config: PaperOnlySurvivorRegistryConfig
) -> dict[str, Any]:
    strategy_id = str(source.get("strat") or source.get("strategy_id") or "")
    exchange = str(source.get("exch") or source.get("exchange") or "")
    symbol = str(source.get("sym") or source.get("symbol") or "")
    timeframe = str(source.get("tf") or source.get("timeframe") or "")
    samples = _int(source.get("n"))
    taker = _route_metrics(source.get("taker"))
    maker = _route_metrics(source.get("maker"))
    state, action, route, why = _classify(
        samples=samples,
        taker=taker,
        maker=maker,
        error=source.get("error"),
        config=config,
    )
    selected = taker if route == "taker" else maker
    lane_id = _lane_id(strategy_id, exchange, symbol, timeframe)
    return {
        "lane_id": lane_id,
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "survivor_state": state,
        "action": action,
        "selected_route": route,
        "why": why,
        "samples": samples,
        "selected_net_usd": round(selected["net"], 6),
        "selected_profit_factor": selected["pf"],
        "selected_avg_net_bps": selected["avg_net_bps"],
        "taker": taker,
        "maker": maker,
        "score": _score(samples=samples, route=route, selected=selected, state=state, config=config),
        "strategy_params": _strategy_params(strategy_id),
        "backtest_rows": _int(source.get("rows")),
        "exit_model": source.get("exit_model"),
        "trail_atr_mult": source.get("trail_atr_mult"),
        "error": str(source.get("error") or ""),
        "can_trade": False,
        "can_promote": False,
    }


def _classify(
    *,
    samples: int,
    taker: Mapping[str, float],
    maker: Mapping[str, float],
    error: Any,
    config: PaperOnlySurvivorRegistryConfig,
) -> tuple[str, str, str, str]:
    if error:
        return STATE_BACKTEST_ERROR, ACTION_REPAIR_BACKTEST, "none", str(error)
    if samples <= 0:
        return STATE_NO_TRADE_SAMPLE, ACTION_WAIT_FOR_SAMPLE, "none", "no trades in backtest cell"
    taker_pass = _route_pass(samples, taker, config=config)
    maker_pass = _route_pass(samples, maker, config=config)
    if taker_pass:
        return (
            STATE_PAPER_SURVIVOR,
            ACTION_RUN_PAPER_ONLY,
            "taker",
            "strict taker route clears sample, PF, and fee-wall gates",
        )
    if maker_pass and taker["avg_net_bps"] >= config.maker_probe_min_taker_bps:
        return (
            STATE_PAPER_MAKER_PROBE,
            ACTION_RUN_PAPER_MAKER_PROBE,
            "maker",
            "maker upper-bound clears gates; paper must prove live fill quality",
        )
    return (
        STATE_PAPER_QUARANTINE,
        ACTION_QUARANTINE_PAPER,
        "taker" if taker["net"] >= maker["net"] else "maker",
        "backtest cell fails sample/PF/avg-bps survivor gates",
    )


def _route_pass(
    samples: int,
    route: Mapping[str, float],
    *,
    config: PaperOnlySurvivorRegistryConfig,
) -> bool:
    return (
        samples >= config.min_trades
        and route["net"] > 0
        and route["pf"] >= config.min_profit_factor
        and route["avg_net_bps"] >= config.min_avg_net_bps
    )


def _summary(
    rows: list[dict[str, Any]], *, config: PaperOnlySurvivorRegistryConfig
) -> dict[str, Any]:
    states = Counter(str(row.get("survivor_state") or "") for row in rows)
    actions = Counter(str(row.get("action") or "") for row in rows)
    roster = [
        row
        for row in rows
        if row.get("survivor_state") in {STATE_PAPER_SURVIVOR, STATE_PAPER_MAKER_PROBE}
    ]
    return {
        "total_cells": len(rows),
        "paper_survivors": states[STATE_PAPER_SURVIVOR],
        "paper_maker_probes": states[STATE_PAPER_MAKER_PROBE],
        "paper_quarantine": states[STATE_PAPER_QUARANTINE],
        "backtest_errors": states[STATE_BACKTEST_ERROR],
        "no_trade_sample": states[STATE_NO_TRADE_SAMPLE],
        "proposed_paper_lanes": min(len(roster), config.max_paper_lanes),
        "state_counts": dict(sorted(states.items())),
        "action_counts": dict(sorted(actions.items())),
        "can_trade": False,
        "can_promote": False,
    }


def _proposed_roster(
    rows: list[dict[str, Any]], *, config: PaperOnlySurvivorRegistryConfig
) -> dict[str, Any]:
    paper_rows = [
        _slim(row)
        for row in rows
        if row.get("survivor_state") in {STATE_PAPER_SURVIVOR, STATE_PAPER_MAKER_PROBE}
    ][: max(1, int(config.max_paper_lanes))]
    return {
        "paper_lanes": paper_rows,
        "strict_survivors": [
            _slim(row) for row in rows if row.get("survivor_state") == STATE_PAPER_SURVIVOR
        ],
        "maker_probes": [
            _slim(row) for row in rows if row.get("survivor_state") == STATE_PAPER_MAKER_PROBE
        ],
        "paper_quarantine": [
            _slim(row) for row in rows if row.get("survivor_state") == STATE_PAPER_QUARANTINE
        ],
        "policy": "paper_only_survivors_from_pre_registered_backtest_grid",
    }


def _boards(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "paper_survivors": [
            _slim(row) for row in rows if row.get("survivor_state") == STATE_PAPER_SURVIVOR
        ],
        "maker_probes": [
            _slim(row) for row in rows if row.get("survivor_state") == STATE_PAPER_MAKER_PROBE
        ],
        "paper_quarantine": [
            _slim(row) for row in rows if row.get("survivor_state") == STATE_PAPER_QUARANTINE
        ],
        "fix_backtest": [
            _slim(row) for row in rows if row.get("survivor_state") == STATE_BACKTEST_ERROR
        ],
    }


def _slim(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "lane_id": row.get("lane_id"),
        "exchange": row.get("exchange"),
        "symbol": row.get("symbol"),
        "timeframe": row.get("timeframe"),
        "strategy_id": row.get("strategy_id"),
        "strategy_params": row.get("strategy_params") or {},
        "survivor_state": row.get("survivor_state"),
        "action": row.get("action"),
        "selected_route": row.get("selected_route"),
        "samples": row.get("samples"),
        "selected_profit_factor": row.get("selected_profit_factor"),
        "selected_avg_net_bps": row.get("selected_avg_net_bps"),
        "score": row.get("score"),
        "why": row.get("why"),
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    survivors = _int(summary.get("paper_survivors"))
    probes = _int(summary.get("paper_maker_probes"))
    quarantine = _int(summary.get("paper_quarantine"))
    errors = _int(summary.get("backtest_errors"))
    if survivors or probes:
        return (
            f"{survivors} strict survivor(s), {probes} maker-probe lane(s). "
            "Use the proposed roster for PAPER-only forward testing; quarantine the rest."
        )
    if errors:
        return f"{errors} backtest cell(s) need repair before the paper-only roster is trustworthy."
    if quarantine:
        return f"{quarantine} tested lane(s) are quarantined; no paper survivor is proven yet."
    return "No backtest survivor evidence is available yet."


def _route_metrics(value: Any) -> dict[str, float]:
    row = value if isinstance(value, Mapping) else {}
    return {
        "net": round(_float(row.get("net")), 6),
        "pf": round(_float(row.get("pf")), 6),
        "win": round(_float(row.get("win")), 6),
        "dd": round(_float(row.get("dd")), 6),
        "avg_net_bps": round(_float(row.get("avg_net_bps")), 6),
    }


def _score(
    *,
    samples: int,
    route: str,
    selected: Mapping[str, float],
    state: str,
    config: PaperOnlySurvivorRegistryConfig,
) -> float:
    if state in {STATE_BACKTEST_ERROR, STATE_NO_TRADE_SAMPLE}:
        return 0.0
    sample = min(30.0, samples / max(1, config.min_trades) * 30.0)
    pf = min(30.0, selected["pf"] / max(0.0001, config.min_profit_factor) * 30.0)
    bps = min(30.0, selected["avg_net_bps"] / max(0.0001, config.min_avg_net_bps) * 30.0)
    route_bonus = 10.0 if route == "taker" else 4.0
    if state == STATE_PAPER_QUARANTINE:
        route_bonus = 0.0
    return round(max(0.0, min(100.0, sample + pf + bps + route_bonus)), 2)


def _strategy_params(strategy_id: str) -> dict[str, Any]:
    # Keep the registry executable by the existing multi-lane builder. Most
    # strategy defaults are encoded in their classes; fee-wall/taker routing is
    # tested through the lane's selected_route metadata, not hidden params.
    if strategy_id == "crypto_trend_atr_margin_v1":
        return {"take_profit_r": None}
    return {}


def _lane_id(strategy_id: str, exchange: str, symbol: str, timeframe: str) -> str:
    return f"paperonly_{strategy_id}_{exchange}_{_slug(symbol)}_{timeframe}"


def _slug(value: str) -> str:
    return (
        str(value)
        .lower()
        .replace("/", "_")
        .replace(":", "_")
        .replace("-", "_")
    )


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, float, str]:
    priority = {
        STATE_PAPER_SURVIVOR: 0,
        STATE_PAPER_MAKER_PROBE: 1,
        STATE_PAPER_QUARANTINE: 2,
        STATE_NO_TRADE_SAMPLE: 3,
        STATE_BACKTEST_ERROR: 4,
    }.get(str(row.get("survivor_state") or ""), 9)
    return (
        priority,
        -_float(row.get("score")),
        -_float(row.get("selected_avg_net_bps")),
        str(row.get("lane_id") or ""),
    )


def _read_json_payload(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", type=Path, default=DEFAULT_GRID)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--min-profit-factor", type=float, default=1.5)
    parser.add_argument("--min-avg-net-bps", type=float, default=25.0)
    parser.add_argument("--maker-probe-min-taker-bps", type=float, default=-10.0)
    parser.add_argument("--max-paper-lanes", type=int, default=18)
    parser.add_argument("--max-rows", type=int, default=240)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = PaperOnlySurvivorRegistryConfig(
        min_trades=args.min_trades,
        min_profit_factor=args.min_profit_factor,
        min_avg_net_bps=args.min_avg_net_bps,
        maker_probe_min_taker_bps=args.maker_probe_min_taker_bps,
        max_paper_lanes=args.max_paper_lanes,
        max_rows=args.max_rows,
    )
    while True:
        payload = build_paper_only_survivor_registry(
            grid_path=args.grid,
            config=config,
        )
        publish_paper_only_survivor_registry(payload, args.out, args.feed)
        print(render_report(payload), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
