"""Continuous research loop — the slow loop, made continuous.

    python -m vnedge.research.continuous_research

Every cycle (default hourly — walk-forward output only changes when a new
candle lands):

  1. refresh candles + funding via REST (incremental; full 365d backfill on
     first run), through the same quality gate as every other ingest
  2. re-run walk-forward for every registered strategy family x symbol on
     the rolling window
  3. evaluate promotion gates (sparse gates for event strategies, standard
     otherwise) and publish verdicts to research/live_research/latest.json
     (atomic) + an append-only feed.jsonl

Governance: this process has NO access to execution. It shares nothing with
the trial container except read-only market data and the research output
directory. Gates are the same frozen objects the judgment runs use; a PASS
here is a *candidate* signal prompting a proper pre-registered judgment on
untouched data — never an auto-promotion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import (
    SPARSE_STRATEGY_GATES,
    PromotionGates,
    WalkForwardResult,
    evaluate_promotion,
    param_grid,
    walk_forward,
)
from vnedge.data.candle_ingestor import ingest_candles
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.funding_ingestor import ingest_funding
from vnedge.data.parquet_store import ParquetStore
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.trend_continuation import TrendContinuation

logger = logging.getLogger(__name__)

EXCHANGE = "binanceusdm"
SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
TIMEFRAME = "1h"
LOOKBACK_DAYS = 365
INTERVAL_SECONDS = float(os.environ.get("RESEARCH_INTERVAL_SECONDS", "3600"))
OUT_DIR = Path("research/live_research")


def wf_record(
    strategy: str, symbol: str, result: WalkForwardResult, gates: PromotionGates,
) -> dict:
    decision = evaluate_promotion(result, gates)
    trades = sum(w.test_metrics.num_trades for w in result.windows)
    traded = sum(1 for w in result.windows if w.test_metrics.num_trades > 0)
    return {
        "strategy": strategy,
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "windows": len(result.windows),
        "traded_windows": traded,
        "oos_trades": trades,
        "oos_net_usd": round(result.oos_net_profit_usd, 2),
        "profitable_windows_pct": round(result.oos_profitable_window_pct, 1),
        "verdict": "PASS" if decision.passed else "REJECT",
        "reasons": list(decision.reject_reasons),
        "updated": datetime.now(UTC).isoformat(),
    }


async def refresh_data(store: ParquetStore, symbol: str) -> bool:
    """Incremental refresh through the quality gate; full backfill if the
    store is empty. Returns False when the gate rejects (research skips the
    symbol this cycle — fail closed, never research on bad data)."""
    until_ms = int(time.time() * 1000)
    try:
        candles = store.read_candles(EXCHANGE, symbol, TIMEFRAME)
        since_ms = int(candles["timestamp"].iloc[-1].value // 1_000_000) - 2 * 3_600_000
    except FileNotFoundError:
        since_ms = until_ms - LOOKBACK_DAYS * 86_400_000
        logger.info("%s: no local data — full %dd backfill", symbol, LOOKBACK_DAYS)

    async with CcxtPublicClient(EXCHANGE) as client:
        c = await ingest_candles(
            client, store, symbol=symbol, timeframe=TIMEFRAME,
            since_ms=since_ms, until_ms=until_ms,
        )
        f = await ingest_funding(
            client, store, symbol=symbol, since_ms=since_ms, until_ms=until_ms,
        )
    if not (c.persisted and f.persisted):
        logger.error("%s: quality gate rejected refresh (%s / %s)",
                     symbol, c.report.summary, f.report.summary)
        return False
    return True


def run_walk_forwards(store: ParquetStore, symbol: str) -> list[dict]:
    candles = store.read_candles(EXCHANGE, symbol, TIMEFRAME)
    funding = store.read_funding(EXCHANGE, symbol)
    cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=LOOKBACK_DAYS)
    c = candles[candles["timestamp"] >= cutoff].reset_index(drop=True)
    f = funding[funding["timestamp"] >= cutoff].reset_index(drop=True)
    config = BacktestConfig()
    records = []

    result = walk_forward(
        c, f, lambda **p: FundingMeanReversion(funding=f, **p),
        param_grid(extreme_pct=[0.85, 0.95], z_entry=[1.5, 2.5]),
        config, train_bars=1440, test_bars=720, symbol=symbol, timeframe=TIMEFRAME,
    )
    records.append(
        wf_record("funding_mean_reversion_v1", symbol, result, SPARSE_STRATEGY_GATES)
    )

    result = walk_forward(
        c, f, lambda **p: TrendContinuation(funding=f, **p),
        param_grid(breakout_bars=[48, 96], take_profit_r=[2.0, 3.0]),
        config, train_bars=1440, test_bars=360, symbol=symbol, timeframe=TIMEFRAME,
    )
    records.append(
        wf_record("trend_continuation_v1", symbol, result, PromotionGates())
    )
    return records


def publish(records: list[dict], started: float) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cycle_seconds": round(time.time() - started, 1),
        "lookback_days": LOOKBACK_DAYS,
        "note": "rolling exploratory walk-forward — a PASS is a candidate "
                "signal, not a promotion; judgment requires untouched data",
        "results": records,
    }
    tmp = OUT_DIR / "latest.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(OUT_DIR / "latest.json")
    with open(OUT_DIR / "feed.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


async def run_cycle() -> list[dict]:
    started = time.time()
    store = ParquetStore("data")
    records: list[dict] = []
    for symbol in SYMBOLS:
        try:
            if not await refresh_data(store, symbol):
                continue
            records.extend(run_walk_forwards(store, symbol))
        except Exception as exc:  # noqa: BLE001 — one symbol must not kill the loop
            logger.exception("research cycle failed for %s: %s", symbol, exc)
    publish(records, started)
    for r in records:
        logger.info("%s %s: %s (oos $%+.2f, %d trades, %d windows)",
                    r["strategy"], r["symbol"], r["verdict"],
                    r["oos_net_usd"], r["oos_trades"], r["windows"])
    return records


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logger.info("continuous research loop: %s every %.0fs", SYMBOLS, INTERVAL_SECONDS)
    while True:
        try:
            await run_cycle()
        except Exception as exc:  # noqa: BLE001
            logger.exception("cycle crashed: %s", exc)
        await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
