"""Multi-exchange lanes entry point.

    DASHBOARD_TOKEN=... python -m vnedge.runtime.multi_lane_shadow

Runs fully isolated lanes on Binance, Bybit, and Delta. Binance/Bybit run the
governed funding-MR paper/shadow lanes; Delta runs a candle-only trend shadow
lane plus a funding-MR shadow lane that accumulates funding live off its native
websocket (no REST funding history on Delta) and warms up until the percentile
window fills. All lanes use live public market data, $500 imaginary base, and
NO live orders.

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
TREND_PARAMS: dict = {}

DEFAULT_PRIMARY_LANE_ID = "funding_mr_btc_v1_20260703"
DEFAULT_BYBIT_BTC_LANE_ID = "funding_mr_bybit_20260704"
DEFAULT_EXCHANGES = "binanceusdm,bybit,delta_india"
DELTA_EXCHANGE = "delta_india"

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

# Exploratory candidate lanes surfaced by the edge-research agent — run in
# SHADOW only (gateway-evaluated intents journaled, NEVER a fill) so their live
# behaviour can be watched without committing capital. A rolling PASS on
# already-seen data is a candidate, NOT a promotion: each still needs a
# human-approved, pre-registered judgment on UNTOUCHED data before it could
# ever reach a paper lane. Toggle with MULTI_LANE_CANDIDATES=0.
#
# trend_continuation_v1 on XRP was the standout of the 2026-07-04 sweep:
# OOS +$92.20 (bybit) / +$80.25 (binance), 53-57 trades, but gate-REJECTED —
# so it is explicitly not promotable, only observable. Default trend config.
CANDIDATE_SHADOW_LANES = [
    LaneSpec(
        lane_id="trend_continuation_xrp_bybit_shadow",
        exchange="bybit", symbol="XRP/USDT:USDT",
        strategy_id="trend_continuation_v1",
        strategy_params={},          # default trend_continuation_v1 config
        mode=RunnerMode.SHADOW,
    ),
]


def _truthy(environ: Mapping[str, str], name: str, default: str) -> bool:
    return environ.get(name, default).lower() in ("1", "true", "yes", "on")


def manifest_shadow_lanes(environ: Mapping[str, str] = os.environ) -> list[LaneSpec]:
    """Shadow lanes auto-generated from research winners (shadow_lanes.json).

    ON by default (MULTI_LANE_MANIFEST_ENABLED=0 opts out): only lanes with
    human-locked params reach the manifest; all are shadow-only, can_trade=false
    research candidates. Paper/live promotion still needs explicit human
    approval and untouched-data judgment.
    """
    if not _truthy(environ, "MULTI_LANE_MANIFEST_ENABLED", "1"):
        return []
    from vnedge.research.shadow_manifest import load_shadow_manifest
    out_dir = Path(environ.get("MULTI_LANE_RESEARCH_DIR", "research/live_research"))
    specs: list[LaneSpec] = []
    for lane in load_shadow_manifest(out_dir).get("lanes", []):
        try:
            specs.append(LaneSpec(
                lane_id=lane["lane_id"], exchange=lane["exchange"],
                symbol=lane["symbol"], timeframe=lane.get("timeframe", "1h"),
                strategy_id=lane["strategy_id"],
                strategy_params=lane.get("strategy_params") or {},
                mode=RunnerMode.SHADOW,
            ))
        except (KeyError, TypeError):
            continue
    return specs


def _lane_identity(spec: LaneSpec) -> tuple[str, str, str, str, RunnerMode]:
    """Semantic lane key; protects against duplicate ids for the same lane."""
    return (
        spec.exchange,
        spec.symbol,
        spec.timeframe,
        spec.strategy_id,
        spec.mode,
    )


def dedupe_lane_specs(specs: list[LaneSpec]) -> list[LaneSpec]:
    """Preserve first occurrence, dropping duplicate ids or semantic twins."""
    out: list[LaneSpec] = []
    seen_ids: set[str] = set()
    seen_identities: set[tuple[str, str, str, str, RunnerMode]] = set()
    for spec in specs:
        identity = _lane_identity(spec)
        if spec.lane_id in seen_ids or identity in seen_identities:
            logger.info(
                "skipping duplicate lane %s (%s %s %s %s)",
                spec.lane_id, spec.exchange, spec.symbol,
                spec.strategy_id, spec.mode.value,
            )
            continue
        out.append(spec)
        seen_ids.add(spec.lane_id)
        seen_identities.add(identity)
    return out


def candidate_shadow_lanes(environ: Mapping[str, str] = os.environ) -> list[LaneSpec]:
    if not _truthy(environ, "MULTI_LANE_CANDIDATES", "1"):
        return []
    return dedupe_lane_specs(list(CANDIDATE_SHADOW_LANES) + manifest_shadow_lanes(environ))


def delta_funding_mr_lanes(environ: Mapping[str, str] = os.environ) -> list[LaneSpec]:
    """Delta India funding-MR — SHADOW only, accumulating funding live.

    Delta exposes no REST funding history, so this lane builds the percentile
    window purely from live funding observations (persisted across restarts via
    the funding accumulator) and warms up — emitting NO signal — until the
    window fills. Same frozen human-approved params as the governed lanes; no
    fills, $500 imaginary base. Toggle with MULTI_LANE_DELTA_FUNDING_MR=0.
    """
    if not _truthy(environ, "MULTI_LANE_DELTA_FUNDING_MR", "1"):
        return []
    if DELTA_EXCHANGE not in _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ):
        return []
    symbol = "BTC/USD:USD"
    return [
        LaneSpec(
            lane_id=f"funding_mr_{DELTA_EXCHANGE}_{_slug_symbol(symbol)}_shadow",
            exchange=DELTA_EXCHANGE,
            symbol=symbol,
            timeframe=environ.get("MULTI_LANE_TIMEFRAME", "1h"),
            strategy_id="funding_mean_reversion_v1",
            strategy_params=FUNDING_MR_PARAMS,
            mode=RunnerMode.SHADOW,
        )
    ]


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


def _env_suffix(exchange: str) -> str:
    return exchange.upper().replace("-", "_").replace("/", "_")


def _delta_india_symbol(symbol: str) -> str:
    if "/USDT" not in symbol:
        return symbol
    base = symbol.split("/", maxsplit=1)[0]
    return f"{base}/USD:USD"


def _symbols_for_exchange(
    exchange: str,
    symbols: list[str],
    environ: Mapping[str, str],
) -> list[str]:
    override = _csv_env(f"MULTI_LANE_SYMBOLS_{_env_suffix(exchange)}", "", environ)
    if override:
        return override
    if exchange == DELTA_EXCHANGE:
        return [_delta_india_symbol(symbol) for symbol in symbols]
    return symbols


def _lane_prefix(strategy_id: str) -> str:
    if strategy_id == "funding_mean_reversion_v1":
        return "funding_mr"
    if strategy_id == "trend_continuation_v1":
        return "trend_continuation"
    return strategy_id


def _lane_id(exchange: str, symbol: str, mode: RunnerMode, strategy_id: str) -> str:
    if strategy_id == "funding_mean_reversion_v1" and mode is RunnerMode.PAPER:
        governed = GOVERNED_PAPER_LANE_IDS.get((exchange, symbol))
        if governed is not None:
            return governed
    return f"{_lane_prefix(strategy_id)}_{exchange}_{_slug_symbol(symbol)}_{mode.value}"


def _default_strategy_for_exchange(exchange: str) -> str:
    if exchange == DELTA_EXCHANGE:
        return "trend_continuation_v1"
    return "funding_mean_reversion_v1"


def _default_params_for_strategy(strategy_id: str) -> dict:
    if strategy_id == "funding_mean_reversion_v1":
        return FUNDING_MR_PARAMS
    if strategy_id == "trend_continuation_v1":
        return TREND_PARAMS
    return {}


def _modes_for_exchange(
    exchange: str, modes: list[RunnerMode], environ: Mapping[str, str]
) -> list[RunnerMode]:
    if exchange != DELTA_EXCHANGE:
        return modes
    if _truthy(environ, "MULTI_LANE_DELTA_PAPER", "0"):
        return modes
    # Delta India has no funding history and no CCXT Pro websocket in the current
    # adapter set, so default it to observe-only shadow. Operators can opt in
    # to simulated paper explicitly after choosing a Delta-ready strategy.
    return [mode for mode in modes if mode is RunnerMode.SHADOW]


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


def build_lane_specs_from_env(
    environ: Mapping[str, str] = os.environ,
) -> list[LaneSpec]:
    exchanges = _csv_env("MULTI_LANE_EXCHANGES", DEFAULT_EXCHANGES, environ)
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

    specs: list[LaneSpec] = []
    for exchange in exchanges:
        strategy_id = _default_strategy_for_exchange(exchange)
        strategy_params = _default_params_for_strategy(strategy_id)
        exchange_modes = _modes_for_exchange(exchange, modes, environ)
        if not exchange_modes:
            logger.warning(
                "%s produced no lanes for modes=%s; use shadow or set "
                "MULTI_LANE_DELTA_PAPER=1 deliberately",
                exchange,
                ",".join(mode.value for mode in modes),
            )
            continue
        for symbol in _symbols_for_exchange(exchange, symbols, environ):
            for mode in exchange_modes:
                specs.append(LaneSpec(
                    lane_id=_lane_id(exchange, symbol, mode, strategy_id),
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
                    strategy_id=strategy_id,
                    strategy_params=strategy_params,
                    mode=mode,
                ))
    if not specs:
        raise ValueError("multi-lane configuration produced no runnable lanes")
    if not any(spec.is_primary for spec in specs):
        specs[0] = replace(specs[0], is_primary=True)
    return specs


async def main() -> None:
    journal_dir = Path(os.environ.get("MULTI_LANE_JOURNAL_DIR", "logs/paper_trials"))
    lanes = dedupe_lane_specs(
        build_lane_specs_from_env()
        + candidate_shadow_lanes()
        + delta_funding_mr_lanes()
    )
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
            alpha_council_path=Path("research/live_research/alpha_council_latest.json"),
            alpha_workbench_path=Path("research/live_research/alpha_workbench_latest.json"),
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
