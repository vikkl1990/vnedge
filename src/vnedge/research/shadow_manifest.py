"""Research-to-shadow lane manifest.

This is the handoff between the slow research loop and the live-data shadow
workspace. It deliberately produces shadow-only lane specs: agents may point
at promising exchange/symbol/strategy combinations, but they still cannot
trade, promote, or mutate a governed paper trial.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from vnedge.research.universe import profitable_pairs

RUNTIME_LOCKED_PARAMS = {
    "funding_mean_reversion_v1": {
        "extreme_pct": 0.85,
        "z_entry": 1.5,
        "funding_pct_window": 240,
        "z_window": 48,
        "stop_atr_mult": 1.5,
    },
}


def _slug(value: str) -> str:
    return value.lower().replace("/", "_").replace(":", "_").replace("-", "_")


def _lane_id(strategy_id: str, exchange: str, symbol: str, timeframe: str) -> str:
    strategy = strategy_id.removesuffix("_v1")
    return f"{strategy}_{exchange}_{_slug(symbol)}_{_slug(timeframe)}_shadow"


def _matching_record(records: Iterable[dict], pair: dict) -> dict | None:
    for record in records:
        if (
            not record.get("auto")
            and record.get("exchange", "binanceusdm") == pair["exchange"]
            and record.get("symbol") == pair["symbol"]
            and record.get("timeframe", "1h") == pair["timeframe"]
            and record.get("strategy") == pair["best_strategy"]
        ):
            return record
    return None


def _selected_params_summary(record: dict | None) -> dict:
    return dict(record.get("selected_params") or {}) if record else {}


def build_shadow_lane_manifest(
    research_payload: dict,
    *,
    max_lanes: int = 12,
    include_rejects: bool = False,
    starting_equity: float = 500.0,
    daily_loss_usd: float = 10.0,
) -> dict:
    records = list(research_payload.get("results", []))
    agent_pairs = (
        research_payload.get("edge_agents", {}).get("profitable_pairs")
        or [p.to_dict() for p in profitable_pairs(records)]
    )
    lanes: list[dict] = []
    blocked: list[dict] = []

    for pair in agent_pairs:
        verdict = pair.get("verdict", "REJECT")
        strategy_id = pair["best_strategy"]
        record = _matching_record(records, pair)
        base = {
            "exchange": pair["exchange"],
            "symbol": pair["symbol"],
            "timeframe": pair.get("timeframe", "1h"),
            "strategy_id": strategy_id,
            "verdict": verdict,
            "oos_net_usd": pair.get("oos_net_usd", 0.0),
            "oos_trades": pair.get("oos_trades", 0),
            "gates": pair.get("gates", ""),
            "selected_params": _selected_params_summary(record),
        }
        if verdict != "PASS" and not include_rejects:
            blocked.append({
                **base,
                "runtime_status": "blocked",
                "reason": "rolling lane is profitable but has not passed gates",
            })
            continue
        locked_params = RUNTIME_LOCKED_PARAMS.get(strategy_id)
        if locked_params is None:
            blocked.append({
                **base,
                "runtime_status": "blocked",
                "reason": (
                    "strategy has no human-locked runtime params for shadow deployment"
                ),
            })
            continue
        if len(lanes) >= max_lanes:
            blocked.append({
                **base,
                "runtime_status": "blocked",
                "reason": f"manifest lane cap reached ({max_lanes})",
            })
            continue
        lanes.append({
            **base,
            "lane_id": _lane_id(strategy_id, pair["exchange"], pair["symbol"],
                                pair.get("timeframe", "1h")),
            "mode": "shadow",
            "is_primary": len(lanes) == 0,
            "starting_equity": starting_equity,
            "daily_loss_usd": daily_loss_usd,
            "strategy_params": dict(locked_params),
            "runtime_status": "ready",
            "source_generated_at": research_payload.get("generated_at"),
        })

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_generated_at": research_payload.get("generated_at"),
        "policy": {
            "status": "shadow_only",
            "can_trade": False,
            "can_promote": False,
            "requires_human_approval": True,
            "requires_untouched_judgment": True,
            "requires_param_lock": True,
        },
        "lanes": lanes,
        "blocked_candidates": blocked,
    }


def write_shadow_lane_manifest(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(path)
