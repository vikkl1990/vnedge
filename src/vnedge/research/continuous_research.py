"""Compact, execution-isolated candle research loop.

This process refreshes public market data, evaluates the remaining registered
strategy families with walk-forward testing, and publishes evidence. It has no
order adapter, roster mutation, scanner, or automatic promotion path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from ccxt.base.errors import NotSupported

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import (
    OFFENSIVE_GATES,
    SPARSE_STRATEGY_GATES,
    PromotionGates,
    evaluate_promotion,
    param_grid,
    walk_forward,
)
from vnedge.data.candle_ingestor import ingest_candles
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.funding_ingestor import ingest_funding
from vnedge.data.parquet_store import ParquetStore
from vnedge.research.universe import ResearchTarget, load_research_targets, summarize_universe
from vnedge.risk.protections import STOP_EXIT_REASONS
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.trend_continuation import TrendContinuation
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 365
INTERVAL_SECONDS = float(os.environ.get("RESEARCH_INTERVAL_SECONDS", "3600"))
OUT_DIR = Path("research/live_research")


@dataclass
class ResearchPayload:
    """Non-executable evidence folded into the research document."""

    started: float = 0.0
    records: list[dict] = field(default_factory=list)
    live_shadow_perf: dict = field(default_factory=dict)
    ai_candidates: dict = field(default_factory=dict)
    cascade_reversion: dict = field(default_factory=dict)
    event_taker_replay: dict = field(default_factory=dict)
    leadlag_echo_scalp: dict = field(default_factory=dict)


def _load_optional(name: str) -> dict:
    path = OUT_DIR / name
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_ai_candidates_latest() -> dict:
    return _load_optional("ai_candidates.json")


def _load_cascade_reversion_latest() -> dict:
    return _load_optional("cascade_reversion.json")


def _load_event_taker_latest() -> dict:
    return _load_optional("event_taker_replay.json")


def _load_leadlag_echo_scalp_latest() -> dict:
    return _load_optional("leadlag_echo_scalp.json")


def _empty_funding() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "funding_rate": pd.Series(dtype="float64"),
        }
    )


async def refresh_data(store: ParquetStore, target: ResearchTarget) -> bool:
    """Refresh through ingest quality gates; bad data skips the target."""
    until_ms = int(time.time() * 1000)
    try:
        candles = store.read_candles(target.exchange, target.symbol, target.timeframe)
        since_ms = int(candles["timestamp"].iloc[-1].value // 1_000_000) - 7_200_000
    except FileNotFoundError:
        since_ms = until_ms - LOOKBACK_DAYS * 86_400_000
    funding_since_ms = min(since_ms, until_ms - 48 * 3_600_000)

    async with CcxtPublicClient(target.exchange) as client:
        candle_result = await ingest_candles(
            client,
            store,
            symbol=target.symbol,
            timeframe=target.timeframe,
            since_ms=since_ms,
            until_ms=until_ms,
        )
        funding_ok = True
        try:
            funding_result = await ingest_funding(
                client,
                store,
                symbol=target.symbol,
                since_ms=funding_since_ms,
                until_ms=until_ms,
            )
            funding_ok = funding_result.persisted
        except NotSupported:
            logger.info("%s: funding history unsupported", target.label)
    return bool(candle_result.persisted and funding_ok)


def _enabled_strategies() -> set[str] | None:
    raw = os.environ.get("RESEARCH_STRATEGIES", "").strip()
    return {item.strip() for item in raw.split(",") if item.strip()} if raw else None


def run_walk_forwards(store: ParquetStore, target: ResearchTarget) -> list[dict]:
    candles = store.read_candles(target.exchange, target.symbol, target.timeframe)
    try:
        funding = store.read_funding(target.exchange, target.symbol)
    except FileNotFoundError:
        funding = _empty_funding()
    cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=LOOKBACK_DAYS)
    c = candles[candles["timestamp"] >= cutoff].reset_index(drop=True)
    f = funding[funding["timestamp"] >= cutoff].reset_index(drop=True)
    enabled = _enabled_strategies()

    lanes = (
        (
            "funding_mean_reversion_v1",
            lambda **p: FundingMeanReversion(funding=f, **p),
            param_grid(extreme_pct=[0.85, 0.95], z_entry=[1.5, 2.5]),
            SPARSE_STRATEGY_GATES,
            True,
        ),
        (
            "trend_continuation_v1",
            lambda **p: TrendContinuation(funding=f, **p),
            param_grid(breakout_bars=[48, 96], take_profit_r=[2.0, 3.0]),
            PromotionGates(),
            False,
        ),
        (
            "crypto_trend_atr_margin_v1",
            lambda **p: CryptoTrendAtrMargin(funding=f, **p),
            param_grid(fast_ema=[30], slow_ema=[60], atr_window=[60]),
            PromotionGates(),
            False,
        ),
        (
            "volatility_expansion_breakout_v1",
            lambda **p: VolatilityExpansionBreakout(funding=f, **p),
            param_grid(breakout_bars=[48, 96]),
            OFFENSIVE_GATES,
            False,
        ),
        (
            "panic_reversal_v1",
            lambda **p: PanicReversal(funding=f, **p),
            param_grid(drop_z_entry=[-2.5, -3.0]),
            OFFENSIVE_GATES,
            False,
        ),
        (
            "funding_squeeze_continuation_v1",
            lambda **p: FundingSqueezeContinuation(funding=f, **p),
            param_grid(extreme_pct=[0.88, 0.94]),
            OFFENSIVE_GATES,
            True,
        ),
    )
    records: list[dict] = []
    for strategy_id, factory, grid, gates, funding_required in lanes:
        if enabled is not None and strategy_id not in enabled:
            continue
        if funding_required and f.empty:
            records.append(
                {
                    "strategy": strategy_id,
                    "exchange": target.exchange,
                    "symbol": target.symbol,
                    "timeframe": target.timeframe,
                    "verdict": "UNTESTABLE",
                    "reasons": ["funding history unavailable"],
                    "oos_trades": 0,
                    "oos_net_usd": 0.0,
                }
            )
            continue
        result = walk_forward(
            c,
            f,
            factory,
            grid,
            BacktestConfig(),
            train_bars=1440,
            test_bars=720,
            symbol=target.symbol,
            timeframe=target.timeframe,
        )
        decision = evaluate_promotion(result, gates)
        records.append(
            {
                "strategy": strategy_id,
                "exchange": target.exchange,
                "symbol": target.symbol,
                "timeframe": target.timeframe,
                "verdict": "PASS" if decision.passed else "REJECT",
                "reasons": list(decision.reject_reasons),
                "oos_trades": sum(w.test_metrics.num_trades for w in result.windows),
                "oos_net_usd": round(result.oos_net_profit_usd, 2),
                "windows": len(result.windows),
            }
        )
    return records


def wf_record(
    strategy: str,
    symbol: str,
    result,
    gates,
    *,
    exchange: str = "binanceusdm",
    timeframe: str = "1h",
) -> dict:
    """Compact walk-forward record retained for diagnostics integrations."""
    all_trades = [trade for window in result.windows for trade in window.test_trades]
    max_stops = current = 0
    for trade in all_trades:
        if trade.exit_reason in STOP_EXIT_REASONS:
            current += 1
            max_stops = max(max_stops, current)
        else:
            current = 0
    decision = evaluate_promotion(result, gates)
    return {
        "strategy": strategy,
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "verdict": "PASS" if decision.passed else "REJECT",
        "reasons": list(decision.reject_reasons),
        "windows": len(result.windows),
        "oos_trades": len(all_trades),
        "oos_net_usd": round(result.oos_net_profit_usd, 2),
        "max_consecutive_stops": max_stops,
    }


def publish(
    value: ResearchPayload | list[dict],
    targets: tuple[ResearchTarget, ...] = (),
) -> None:
    """Atomically publish evidence; publication never changes runtime roster."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    folded = value if isinstance(value, ResearchPayload) else ResearchPayload(records=value)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": {
            "measurement_first": True,
            "can_trade": False,
            "can_promote": False,
            "capital_roster_mutation": False,
        },
        "universe": summarize_universe(targets),
        "results": folded.records,
        "live_shadow_perf": folded.live_shadow_perf,
        "ai_candidates": folded.ai_candidates,
        "cascade_reversion": folded.cascade_reversion,
        "event_taker_replay": folded.event_taker_replay,
        "leadlag_echo_scalp": folded.leadlag_echo_scalp,
    }
    tmp = OUT_DIR / "latest.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, OUT_DIR / "latest.json")
    with (OUT_DIR / "feed.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


async def run_cycle() -> list[dict]:
    store = ParquetStore("data")
    targets = load_research_targets()
    records: list[dict] = []
    for target in targets:
        try:
            if await refresh_data(store, target):
                records.extend(run_walk_forwards(store, target))
        except Exception:
            logger.exception("research target %s failed", target.label)
    publish(records, targets)
    return records


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        await run_cycle()
        await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
