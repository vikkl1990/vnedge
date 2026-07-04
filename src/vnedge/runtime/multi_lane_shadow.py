"""Multi-exchange lanes entry point.

    DASHBOARD_TOKEN=... python -m vnedge.runtime.multi_lane_shadow

Runs funding-MR as parallel, fully isolated lanes on Binance and Bybit, both
on live data, both $500 imaginary base, NO live orders. Each venue runs in
BOTH fill modes side by side:

  - PAPER  — simulated fills on live data; produces the live-$ venue
             comparison and keeps the human-approved funding_mr_btc_v1_20260703
             trial (Binance) + funding_mr_bybit_20260704 (Bybit) progressing
             toward their pre-registered verdicts, reusing their account files.
  - SHADOW — gateway-evaluated intents journaled per venue, never a fill;
             a pure signal/decision record independent of position state.

Neither mode ever submits to a real exchange. Modes/venues/symbols are env
driven (MULTI_LANE_MODES, MULTI_LANE_EXCHANGES, MULTI_LANE_SYMBOLS).
"""

from __future__ import annotations

import asyncio
import json
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
from vnedge.runtime.runner_config import RunnerMode

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

# The human-approved PAPER trials, keyed by (exchange, symbol). Their PAPER
# lane reuses these exact ids so it continues the governed trial (account
# files + pre-registered verdict). Shadow/other lanes get derived ids.
GOVERNED_PAPER_LANE_IDS = {
    ("binanceusdm", "BTC/USDT:USDT"): DEFAULT_PRIMARY_LANE_ID,
    ("bybit", "BTC/USDT:USDT"): DEFAULT_BYBIT_BTC_LANE_ID,
}

_MODES: dict[str, RunnerMode] = {
    "paper": RunnerMode.PAPER,
    "shadow": RunnerMode.SHADOW,
}


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


def _lane_id(exchange: str, symbol: str, mode: RunnerMode) -> str:
    if mode is RunnerMode.PAPER:
        governed = GOVERNED_PAPER_LANE_IDS.get((exchange, symbol))
        if governed is not None:
            return governed
        return f"funding_mr_{exchange}_{_slug_symbol(symbol)}_paper"
    return f"funding_mr_{exchange}_{_slug_symbol(symbol)}_shadow"


def _parse_modes(environ: Mapping[str, str]) -> list[RunnerMode]:
    modes: list[RunnerMode] = []
    for name in _csv_env("MULTI_LANE_MODES", "paper,shadow", environ):
        try:
            mode = _MODES[name.lower()]
        except KeyError:
            raise ValueError(
                f"unknown multi-lane mode {name!r} (expected one of {sorted(_MODES)})"
            ) from None
        if mode not in modes:
            modes.append(mode)
    if not modes:
        raise ValueError("at least one multi-lane mode is required")
    return modes


def build_lane_specs_from_manifest(path: Path) -> list[LaneSpec]:
    raw = json.loads(path.read_text())
    lanes = raw.get("lanes", [])
    specs = [
        LaneSpec(
            lane_id=lane["lane_id"],
            exchange=lane["exchange"],
            symbol=lane["symbol"],
            strategy_id=lane.get("strategy_id", "funding_mean_reversion_v1"),
            timeframe=lane.get("timeframe", "1h"),
            starting_equity=float(lane.get("starting_equity", 500.0)),
            daily_loss_usd=float(lane.get("daily_loss_usd", 10.0)),
            is_primary=bool(lane.get("is_primary", False)),
            strategy_params=dict(lane.get("strategy_params", {})),
            mode=RunnerMode(lane.get("mode", "shadow")),
        )
        for lane in lanes
        if lane.get("runtime_status", "ready") == "ready"
    ]
    if not specs:
        raise ValueError(f"shadow manifest {path} has no ready lanes")
    if not any(spec.is_primary for spec in specs):
        specs[0] = replace(specs[0], is_primary=True)
    return specs


def build_lane_specs_from_env(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    manifest = environ.get("MULTI_LANE_MANIFEST")
    if manifest:
        return build_lane_specs_from_manifest(Path(manifest))

    exchanges = _csv_env("MULTI_LANE_EXCHANGES", "binanceusdm,bybit", environ)
    symbols = _csv_env("MULTI_LANE_SYMBOLS", "BTC/USDT:USDT", environ)
    if not exchanges or not symbols:
        raise ValueError("at least one multi-lane exchange and symbol is required")
    modes = _parse_modes(environ)
    timeframe = environ.get("MULTI_LANE_TIMEFRAME", "1h")
    primary_exchange = environ.get("MULTI_LANE_PRIMARY_EXCHANGE", exchanges[0])
    primary_symbol = environ.get("MULTI_LANE_PRIMARY_SYMBOL", symbols[0])
    # The flat top-level dashboard snapshot is the governed PAPER lane by
    # default (live-$ comparison), falling back to the first configured mode.
    default_primary_mode = "paper" if RunnerMode.PAPER in modes else modes[0].value
    primary_mode = _MODES[environ.get("MULTI_LANE_PRIMARY_MODE", default_primary_mode).lower()]
    starting_equity = float(environ.get("MULTI_LANE_STARTING_EQUITY", "500"))
    daily_loss_usd = float(environ.get("MULTI_LANE_DAILY_LOSS_USD", "10"))

    specs = [
        LaneSpec(
            lane_id=_lane_id(exchange, symbol, mode),
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            starting_equity=starting_equity,
            daily_loss_usd=daily_loss_usd,
            is_primary=(
                exchange == primary_exchange
                and symbol == primary_symbol
                and mode is primary_mode
            ),
            strategy_params=FUNDING_MR_PARAMS,
            mode=mode,
        )
        for exchange in exchanges
        for symbol in symbols
        for mode in modes
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

    by_mode: dict[str, int] = {}
    for spec in lanes:
        by_mode[spec.mode.value] = by_mode.get(spec.mode.value, 0) + 1
    logger.info("configured %d lanes (%s); primary=%s", len(lanes),
                ", ".join(f"{n} {m}" for m, n in sorted(by_mode.items())), primary)
    runner = MultiLaneShadowRunner(lanes, journal_dir, provider)
    try:
        await runner.run()
    finally:
        if server_task is not None:
            server_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
