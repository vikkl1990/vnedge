"""Research-winner -> shadow-lane manifest (the research/shadow bridge).

The slow research loop finds profitable candidate pairs; this turns the ones
whose strategy has HUMAN-LOCKED runtime params into shadow-only lane specs the
multi-lane runner can watch live. The lock is the safety line: a profitable
pair whose strategy has no locked params is BLOCKED, not run — we never
auto-spawn a live-data lane on un-vetted parameters.

Shadow-only and non-promoting: every output carries can_trade=false /
can_promote=false. A shadow lane is for observation; promotion to paper/live
still requires a human-approved, pre-registered judgment on untouched data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

# Strategy runtime params a human has locked as safe to run as a SHADOW lane.
# A research winner whose strategy is absent here is surfaced but NOT run —
# that is the line between assistant and overfitter.
RUNTIME_LOCKED_PARAMS: dict[str, dict] = {
    "funding_mean_reversion_v1": {
        "extreme_pct": 0.85, "z_entry": 1.5,
        "funding_pct_window": 240, "z_window": 48, "stop_atr_mult": 1.5,
    },
    "trend_continuation_v1": {},              # default config (judged on DOGE/XRP)
    "volatility_expansion_breakout_v1": {},   # default config (DOGE-judged)
    "alpha_stack_confluence_v1": {},          # research-only confluence default
    "quant_signal_pack_v1": {},               # research-only signal-pack default
}

SHADOW_MANIFEST = "shadow_lanes.json"


def _lane_id(exchange: str, symbol: str, strategy: str) -> str:
    s = symbol.split(":")[0].replace("/", "").lower()
    return f"{strategy}_{exchange}_{s}_shadow"


def generate_shadow_manifest(profitable_pairs, *, max_lanes: int = 12) -> dict:
    """Turn research `profitable_pairs` (dicts) into a shadow-lane manifest.
    Runnable lanes require locked params; the rest are surfaced as blocked."""
    lanes: list[dict] = []
    blocked: list[dict] = []
    seen: set[str] = set()
    for p in profitable_pairs:
        strat = p.get("best_strategy") or p.get("strategy")
        exchange, symbol = p.get("exchange"), p.get("symbol")
        if not (strat and exchange and symbol):
            continue
        if strat not in RUNTIME_LOCKED_PARAMS:
            blocked.append({
                "exchange": exchange, "symbol": symbol, "strategy_id": strat,
                "reason": "no human-locked runtime params — cannot auto-run",
            })
            continue
        lane_id = _lane_id(exchange, symbol, strat)
        if lane_id in seen:
            continue
        seen.add(lane_id)
        lanes.append({
            "lane_id": lane_id,
            "exchange": exchange, "symbol": symbol,
            "timeframe": p.get("timeframe", "1h"),
            "strategy_id": strat,
            "strategy_params": dict(RUNTIME_LOCKED_PARAMS[strat]),
            "mode": "shadow",
            "source_verdict": p.get("verdict"),
            "oos_net_usd": p.get("oos_net_usd"),
        })
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": {
            "can_trade": False, "can_promote": False,
            "requires_untouched_judgment": True, "shadow_only": True,
        },
        "lanes": lanes[:max_lanes],
        "blocked": blocked,
    }


def write_shadow_manifest(manifest: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (SHADOW_MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(out_dir / SHADOW_MANIFEST)   # atomic


def load_shadow_manifest(out_dir: Path) -> dict:
    path = out_dir / SHADOW_MANIFEST
    if not path.exists():
        return {"lanes": [], "blocked": []}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"lanes": [], "blocked": []}
