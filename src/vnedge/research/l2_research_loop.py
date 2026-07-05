"""Decoupled tick/L2 research loop.

Runs the L2-mining discovery passes — scalper discovery and the structural
alpha factory — on their own slower cadence (default 6h) so they never bloat
the hourly candle research loop. Writes research/live_research/l2_latest.json,
which the candle loop folds into its dashboard latest.json.

Research-only, same hard guards as the inline passes: every output carries
can_trade=false / can_promote=false; raw hypotheses are not signals;
conservative replay + untouched-data judgment + human approval remain mandatory
before anything is promoted. This loop discovers; it never trades or promotes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from vnedge.research.alpha_factory import run_alpha_factory
from vnedge.research.continuous_research import (
    OUT_DIR,
    _env_int,
    _scalper_research_days,
    run_scalper_research,
)
from vnedge.research.universe import load_research_targets
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY

logger = logging.getLogger(__name__)

L2_LATEST = "l2_latest.json"


def run_once(data_root: str | Path = "data") -> dict:
    """One decoupled L2-mining pass: scalper discovery + structural alpha."""
    targets = load_research_targets()
    root = Path(data_root)
    days = _scalper_research_days(root, targets)
    scalper = run_scalper_research(data_root, targets, days=days)
    alpha = run_alpha_factory(
        data_root, targets, days=days,
        max_rows=_env_int("ALPHA_FACTORY_MAX_ROWS", 50),
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "days": list(days),
        "scalper_parameter_registry": DEFAULT_SCALPER_PARAMETER_REGISTRY.to_dict(),
        "scalper_research": scalper,
        "alpha_factory": alpha,
    }


def publish_l2(payload: dict, out_dir: Path | None = None) -> None:
    out = out_dir or OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / (L2_LATEST + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(out / L2_LATEST)   # atomic; the candle loop only ever reads whole files


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    interval = _env_int("L2_RESEARCH_INTERVAL_SECONDS", 21600)  # 6h
    logger.info("l2 research loop: every %ds -> %s", interval, OUT_DIR / L2_LATEST)
    while True:
        started = time.time()
        try:
            payload = run_once("data")
            publish_l2(payload)
            sr = payload["scalper_research"]
            af = payload["alpha_factory"]
            logger.info(
                "l2 research: %d edge hyps / %d scanned / %d replay-candidates | "
                "%d alpha hyps | %.0fs",
                len(sr.get("edge_hypotheses", [])),
                len(sr.get("scanner_results", [])),
                len(sr.get("replay_candidates", [])),
                len(af.get("hypotheses", [])) if isinstance(af, dict) else 0,
                time.time() - started,
            )
        except Exception as exc:  # noqa: BLE001 — one cycle must not kill the loop
            logger.exception("l2 research cycle failed: %s", exc)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
