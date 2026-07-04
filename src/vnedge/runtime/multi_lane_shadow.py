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
from collections.abc import Mapping
from dataclasses import replace
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

DEFAULT_PRIMARY_LANE_ID = "funding_mr_btc_v1_20260703"
DEFAULT_BYBIT_BTC_LANE_ID = "funding_mr_bybit_20260704"


def _csv_env(name: str, default: str, environ: Mapping[str, str]) -> list[str]:
    raw = environ.get(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _slug_symbol(symbol: str) -> str:
    return (
        symbol.lower()
        .replace("/", "_")
        .replace(":", "_")
        .replace("-", "_")
    )


def _lane_id(exchange: str, symbol: str) -> str:
    if exchange == "binanceusdm" and symbol == "BTC/USDT:USDT":
        return DEFAULT_PRIMARY_LANE_ID
    if exchange == "bybit" and symbol == "BTC/USDT:USDT":
        return DEFAULT_BYBIT_BTC_LANE_ID
    return f"funding_mr_{exchange}_{_slug_symbol(symbol)}_shadow"


def build_lane_specs_from_env(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    exchanges = _csv_env("MULTI_LANE_EXCHANGES", "binanceusdm,bybit", environ)
    symbols = _csv_env("MULTI_LANE_SYMBOLS", "BTC/USDT:USDT", environ)
    if not exchanges or not symbols:
        raise ValueError("at least one multi-lane exchange and symbol is required")
    timeframe = environ.get("MULTI_LANE_TIMEFRAME", "1h")
    primary_exchange = environ.get("MULTI_LANE_PRIMARY_EXCHANGE", exchanges[0])
    primary_symbol = environ.get("MULTI_LANE_PRIMARY_SYMBOL", symbols[0])
    starting_equity = float(environ.get("MULTI_LANE_STARTING_EQUITY", "500"))
    daily_loss_usd = float(environ.get("MULTI_LANE_DAILY_LOSS_USD", "10"))

    specs = [
        LaneSpec(
            lane_id=_lane_id(exchange, symbol),
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            starting_equity=starting_equity,
            daily_loss_usd=daily_loss_usd,
            is_primary=exchange == primary_exchange and symbol == primary_symbol,
            strategy_params=FUNDING_MR_PARAMS,
        )
        for exchange in exchanges
        for symbol in symbols
    ]
    if not any(spec.is_primary for spec in specs):
        specs[0] = replace(specs[0], is_primary=True)
    return specs


async def main() -> None:
    journal_dir = Path(os.environ.get("MULTI_LANE_JOURNAL_DIR", "logs/paper_trials"))
    lanes = build_lane_specs_from_env()
    primary = next(spec.lane_id for spec in lanes if spec.is_primary)
    provider = MultiLaneProvider(primary_lane_id=primary)

    server_task = None
    token = os.environ.get("DASHBOARD_TOKEN")
    if token:
        import uvicorn

        from vnedge.dashboard.app import create_app

        app = create_app(
            provider, token=token,
            history_path=journal_dir / f"{primary}.equity.jsonl",
            research_path=Path("research/live_research/latest.json"),
        )
        server = uvicorn.Server(uvicorn.Config(
            app, host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
            port=int(os.environ.get("DASHBOARD_PORT", "8080")), log_level="warning"))
        server_task = asyncio.create_task(server.serve())

    logger.info("configured %d shadow lanes; primary=%s", len(lanes), primary)
    runner = MultiLaneShadowRunner(lanes, journal_dir, provider)
    try:
        await runner.run()
    finally:
        if server_task is not None:
            server_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
