"""Measurement-first paper/shadow entrypoint.

The default roster contains observation lanes only. They ingest public venue
data, run DQ/Time-Machine health, journal runtime state, and publish the
dashboard, but their strategy can never emit an order signal.

An optional paper-capital lane needs both an explicit enable flag and a known,
capital-eligible strategy ID. There is no live adapter in this process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from vnedge.runtime.multi_lane import LaneSpec, MultiLaneProvider, MultiLaneShadowRunner
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.strategy_registry import (
    get_strategy_class,
    is_capital_eligible,
    is_shadow_observe_eligible,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_EXCHANGES = "binanceusdm,bybit,delta_india"
DEFAULT_PRIMARY_LANE_ID = "measurement_binanceusdm_btc_usdt_usdt"
DELTA_EXCHANGE = "delta_india"


def _truthy(environ: Mapping[str, str], name: str, default: str = "0") -> bool:
    return str(environ.get(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


def _venue_symbol(exchange: str, symbol: str) -> str:
    if exchange == DELTA_EXCHANGE:
        return symbol.replace("/USDT:USDT", "/USD:USD")
    return symbol


def build_lane_specs_from_env(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Build the always-on, no-signal measurement roster."""
    exchanges = _csv(environ.get("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES))
    symbols = _csv(environ.get("MULTI_LANE_SYMBOLS", "BTC/USDT:USDT"))
    if not exchanges or not symbols:
        raise ValueError("measurement runtime requires at least one exchange and symbol")
    timeframe = environ.get("MULTI_LANE_TIMEFRAME", "1h").strip() or "1h"
    specs: list[LaneSpec] = []
    for exchange in exchanges:
        for configured_symbol in symbols:
            symbol = _venue_symbol(exchange, configured_symbol)
            specs.append(
                LaneSpec(
                    lane_id=f"measurement_{_slug(exchange)}_{_slug(symbol)}",
                    exchange=exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_id="measurement_only_v1",
                    mode=RunnerMode.SHADOW,
                    is_primary=not specs,
                )
            )
    return specs


def build_capital_lane_specs(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Build an explicitly enabled paper roster; empty is the safe default."""
    strategy_id = environ.get("MULTI_LANE_CAPITAL_STRATEGY", "").strip()
    enabled = _truthy(environ, "MULTI_LANE_CAPITAL_ENABLED")
    if not enabled and not strategy_id:
        return []
    if enabled != bool(strategy_id):
        raise ValueError(
            "paper capital requires both MULTI_LANE_CAPITAL_ENABLED=1 and "
            "MULTI_LANE_CAPITAL_STRATEGY"
        )
    get_strategy_class(strategy_id)
    if not is_capital_eligible(strategy_id):
        raise ValueError(f"strategy {strategy_id!r} is not capital eligible")

    exchange = environ.get("MULTI_LANE_CAPITAL_EXCHANGE", "binanceusdm").strip()
    symbol = _venue_symbol(
        exchange,
        environ.get("MULTI_LANE_CAPITAL_SYMBOL", "BTC/USDT:USDT").strip(),
    )
    timeframe = environ.get("MULTI_LANE_CAPITAL_TIMEFRAME", "1h").strip()
    return [
        LaneSpec(
            lane_id=f"paper_{_slug(strategy_id)}_{_slug(exchange)}_{_slug(symbol)}",
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            mode=RunnerMode.PAPER,
            starting_equity=float(environ.get("MULTI_LANE_STARTING_EQUITY", "500")),
            daily_loss_usd=float(environ.get("MULTI_LANE_DAILY_LOSS_USD", "10")),
        )
    ]


def _positive_float(environ: Mapping[str, str], name: str, default: str) -> float:
    raw = str(environ.get(name, default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def _nonnegative_float(environ: Mapping[str, str], name: str, default: str) -> float:
    raw = str(environ.get(name, default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative number") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return value


def build_shadow_observe_lane_specs(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    """Build one explicitly enabled, virtual-outcome-only strategy lane."""
    strategy_id = environ.get("MULTI_LANE_SHADOW_OBSERVE_STRATEGY", "").strip()
    enabled = _truthy(environ, "MULTI_LANE_SHADOW_OBSERVE_ENABLED")
    if not enabled and not strategy_id:
        return []
    if enabled != bool(strategy_id):
        raise ValueError(
            "shadow observe requires both MULTI_LANE_SHADOW_OBSERVE_ENABLED=1 "
            "and MULTI_LANE_SHADOW_OBSERVE_STRATEGY"
        )
    get_strategy_class(strategy_id)
    if not is_shadow_observe_eligible(strategy_id):
        raise ValueError(f"strategy {strategy_id!r} is not shadow-observe eligible")

    exchange = environ.get(
        "MULTI_LANE_SHADOW_OBSERVE_EXCHANGE", "binanceusdm"
    ).strip()
    configured_symbols = _csv(
        environ.get("MULTI_LANE_SHADOW_OBSERVE_SYMBOLS", "").strip()
        or environ.get("MULTI_LANE_SHADOW_OBSERVE_SYMBOL", "BTC/USDT:USDT")
    )
    timeframe = environ.get("MULTI_LANE_SHADOW_OBSERVE_TIMEFRAME", "1h").strip()
    if not exchange or not configured_symbols:
        raise ValueError("shadow observe exchange and symbols cannot be empty")
    if strategy_id == "structure_bos_1h" and timeframe != "1h":
        raise ValueError("structure_bos_1h shadow observe requires timeframe 1h")
    if strategy_id == "fee_wall_momentum_observer_v1" and timeframe != "5m":
        raise ValueError(
            "fee_wall_momentum_observer_v1 shadow observe requires timeframe 5m"
        )
    if strategy_id == "squeeze_expansion_breakout_v2" and timeframe != "5m":
        raise ValueError(
            "squeeze_expansion_breakout_v2 shadow observe requires timeframe 5m"
        )
    starting_equity = _positive_float(
        environ, "MULTI_LANE_SHADOW_OBSERVE_EQUITY", "500"
    )
    daily_loss_usd = _positive_float(
        environ, "MULTI_LANE_SHADOW_OBSERVE_DAILY_LOSS_USD", "10"
    )
    trail_atr_mult = _nonnegative_float(
        environ, "MULTI_LANE_SHADOW_OBSERVE_TRAIL_ATR_MULT", "0"
    )
    return [
        LaneSpec(
            lane_id=f"shadow_observe_{_slug(exchange)}_{_slug(symbol)}",
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            mode=RunnerMode.SHADOW,
            starting_equity=starting_equity,
            daily_loss_usd=daily_loss_usd,
            trail_atr_mult=trail_atr_mult,
            is_primary=False,
        )
        for symbol in (
            _venue_symbol(exchange, configured_symbol)
            for configured_symbol in configured_symbols
        )
    ]


def desired_lane_specs(environ: Mapping[str, str] = os.environ) -> list[LaneSpec]:
    return (
        build_lane_specs_from_env(environ)
        + build_capital_lane_specs(environ)
        + build_shadow_observe_lane_specs(environ)
    )


def build_runtime_control(specs: list[LaneSpec]) -> dict[str, Any]:
    """Publish permission truth independently from lane display labels."""
    paper_lanes = sum(spec.mode is RunnerMode.PAPER for spec in specs)
    observe = [
        spec
        for spec in specs
        if spec.mode is RunnerMode.SHADOW
        and is_shadow_observe_eligible(spec.strategy_id)
    ]
    measurement_lanes = sum(
        spec.strategy_id == "measurement_only_v1" for spec in specs
    )
    return {
        "lane_set_hash": lane_specs_fingerprint(specs),
        "configured_lanes": len(specs),
        "capital_roster_size": paper_lanes,
        "paper_lanes": paper_lanes,
        "shadow_observe_enabled": bool(observe),
        "shadow_observe_strategy": observe[0].strategy_id if observe else None,
        "shadow_observe_lanes": len(observe),
        "measurement_lanes": measurement_lanes,
        "measurement_only_pct": round(100 * measurement_lanes / len(specs), 1),
        "mode_ladder": (
            "measurement/shadow-observe; optional explicit paper; no live adapter"
        ),
        "orders_allowed": paper_lanes > 0,
        "live_orders_allowed": False,
    }


def lane_specs_fingerprint(specs: list[LaneSpec]) -> str:
    payload = [
        {
            "lane_id": spec.lane_id,
            "exchange": spec.exchange,
            "symbol": spec.symbol,
            "timeframe": spec.timeframe,
            "mode": spec.mode.value,
            "strategy_id": spec.strategy_id,
            "strategy_params": spec.strategy_params or {},
        }
        for spec in specs
    ]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


async def main() -> int:
    journal_dir = Path(os.environ.get("MULTI_LANE_JOURNAL_DIR", "logs/paper_trials"))
    lanes = desired_lane_specs()
    primary = next(spec.lane_id for spec in lanes if spec.is_primary)
    runtime_control = build_runtime_control(lanes)
    capital_lanes = int(runtime_control["paper_lanes"])
    observe_lanes = int(runtime_control["shadow_observe_lanes"])
    provider = MultiLaneProvider(
        primary_lane_id=primary,
        lane_specs=lanes,
        journal_dir=journal_dir,
        runtime_control=runtime_control,
    )

    server_task: asyncio.Task | None = None
    from vnedge.dashboard.auth import TokenStore

    token_store = TokenStore.from_env()
    if len(token_store):
        import uvicorn

        from vnedge.dashboard.app import create_app

        app = create_app(
            cast(Any, provider),
            token_store=token_store,
            history_path=journal_dir / f"{primary}.equity.jsonl",
            research_path=Path("research/live_research/latest.json"),
            alerts_path=Path("logs/alerts.jsonl"),
            journal_dir=journal_dir,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
                port=int(os.environ.get("DASHBOARD_PORT", "8080")),
                log_level="warning",
            )
        )
        server_task = asyncio.create_task(server.serve())

    logger.info(
        "configured %d measurement, %d shadow-observe, and %d paper-capital lanes; "
        "primary=%s",
        len(lanes) - capital_lanes - observe_lanes,
        observe_lanes,
        capital_lanes,
        primary,
    )
    try:
        await MultiLaneShadowRunner(lanes, journal_dir, provider).run()
        return 0
    finally:
        if server_task is not None:
            server_task.cancel()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
