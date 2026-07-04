"""Multi-exchange shadow lanes entry point.

    DASHBOARD_TOKEN=... python -m vnedge.runtime.multi_lane_shadow

Runs funding-MR as parallel shadow lanes on Binance and Bybit, both on live
data, both $500 base, no live orders. Serves the read-only dashboard with a
per-lane comparison. The Binance lane continues the governed
funding_mr_btc_v1_20260703 trial (same frozen params + same account files);
the Bybit lane is the comparison venue.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from vnedge.runtime.multi_lane import (
    LaneSpec,
    MultiLaneProvider,
    MultiLaneShadowRunner,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Frozen funding-MR params — the human-approved manifest config (0.85 / 1.5).
FUNDING_MR_PARAMS = {
    "extreme_pct": 0.85,
    "z_entry": 1.5,
    "funding_pct_window": 240,
    "z_window": 48,
    "stop_atr_mult": 1.5,
}

LANES = [
    LaneSpec(lane_id="funding_mr_btc_v1_20260703", exchange="binanceusdm",
             symbol="BTC/USDT:USDT", is_primary=True,
             strategy_params=FUNDING_MR_PARAMS),
    LaneSpec(lane_id="funding_mr_bybit_20260704", exchange="bybit",
             symbol="BTC/USDT:USDT", strategy_params=FUNDING_MR_PARAMS),
]
JOURNAL_DIR = Path("logs/paper_trials")
PRIMARY = "funding_mr_btc_v1_20260703"


async def main() -> None:
    provider = MultiLaneProvider(primary_lane_id=PRIMARY)

    server_task = None
    token = os.environ.get("DASHBOARD_TOKEN")
    if token:
        import uvicorn

        from vnedge.dashboard.app import create_app

        app = create_app(
            provider, token=token,
            history_path=JOURNAL_DIR / f"{PRIMARY}.equity.jsonl",
            research_path=Path("research/live_research/latest.json"),
        )
        server = uvicorn.Server(uvicorn.Config(
            app, host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
            port=int(os.environ.get("DASHBOARD_PORT", "8080")), log_level="warning"))
        server_task = asyncio.create_task(server.serve())

    runner = MultiLaneShadowRunner(LANES, JOURNAL_DIR, provider)
    try:
        await runner.run()
    finally:
        if server_task is not None:
            server_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
