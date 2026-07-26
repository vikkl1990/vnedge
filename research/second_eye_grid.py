"""Independent "second eye" grid backtest of every registered strategy.

Motivation
----------
The fee-wall opportunity-scanner (``fee_wall_forensics``) and the pine-research
pipeline COUNT setups; they do not run a position-aware backtest. This tool is a
deliberately separate code path: it drives each registered strategy through the
real ``vnedge.backtest.backtester.run_backtest`` (sequential single position,
taker fills at next open, adverse slippage, stop-wins-ties) across the full grid
of exchange x symbol x timeframe candle files, so scanner/pipeline claims can be
checked against an honest, well-sampled backtest.

For every cell it reports TWO fee routes computed in one pass:

* ``taker`` - fees as charged by the backtester (venue taker bps).
* ``maker`` - each trade's fee re-scaled analytically to the maker rate
  (``maker_bps / taker_bps``). This tests the scanner's "maker-edge" claim
  WITHOUT re-running, but note it optimistically assumes the maker (limit)
  order fills - real limit entries suffer adverse selection on breakouts, so
  treat the maker column as an upper bound, not a promise.

Honest caveats (do not drop these when reading results):

* SINGLE-WINDOW backtests - no walk-forward, no OOS. PF here is suggestive,
  NOT promotion-grade. Survivors still need walk-forward + a pre-registered
  untouched-window judgment before any promotion.
* A per-cell floor of >=20 trades is the ranking minimum, but >=20 trades on a
  1m file (~260k bars) is still extreme undersampling - discard 1m "winners".
* Funding strategies (funding_mean_reversion, funding_squeeze_continuation)
  are run with funding=None and therefore produce no trades / error here.

Run (inside a container with the vnedge env + normalized candle data)::

    python research/second_eye_grid.py \
        --out research/live_research/second_eye_grid.json
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from vnedge.backtest.backtester import BacktestConfig, run_backtest
from vnedge.backtest.fee_model import FeeModel
from vnedge.strategy.strategy_registry import STRATEGIES

warnings.filterwarnings("ignore")

MAKER_BPS = 2.0
DATA_ROOT = Path("data/normalized")


def _venue_taker_bps(exchange: str) -> float:
    # Bybit standard perp taker is 5.5 bps; Binance USDT-M / Delta India 5.0.
    return 5.5 if exchange == "bybit" else 5.0


def _agg(nets: list[float]) -> dict:
    net = sum(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = 999.0 if gross_win > 0 else 0.0
    equity = np.cumsum(nets)
    max_dd = float((np.maximum.accumulate(equity) - equity).max()) if nets else 0.0
    return {
        "net": round(net, 2),
        "pf": round(min(profit_factor, 999.0), 3),
        "win": round(len(wins) / len(nets) * 100, 1) if nets else 0.0,
        "dd": round(max_dd, 1),
    }


def _cell_metrics(trades, taker_bps: float) -> dict | None:
    if not trades:
        return None
    maker_ratio = MAKER_BPS / taker_bps
    taker_nets, maker_nets = [], []
    for trade in trades:
        gross = trade.gross_pnl_usd
        fee = trade.fees_usd
        funding = trade.funding_usd
        taker_nets.append(gross - fee + funding)
        maker_nets.append(gross - fee * maker_ratio + funding)
    return {"n": len(trades), "taker": _agg(taker_nets), "maker": _agg(maker_nets)}


def _parse_path(path: Path) -> tuple[str, str, str]:
    parts = {}
    for segment in path.parts:
        if "=" in segment:
            key, value = segment.split("=", 1)
            parts[key] = value
    return parts.get("exchange", ""), parts.get("symbol", ""), parts.get("timeframe", "")


def run(out_path: Path) -> list[dict]:
    files = sorted(DATA_ROOT.glob("exchange=*/symbol=*/timeframe=*/candles.parquet"))
    strategy_ids = list(STRATEGIES.keys())
    total = len(strategy_ids) * len(files)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    done = 0
    started = time.time()
    for strategy_id in strategy_ids:
        strategy_cls = STRATEGIES[strategy_id]
        for candle_file in files:
            exchange, symbol, timeframe = _parse_path(candle_file)
            done += 1
            taker_bps = _venue_taker_bps(exchange)
            try:
                candles = pd.read_parquet(candle_file)
                strategy = strategy_cls()
                config = BacktestConfig(
                    fees=FeeModel(taker_bps=taker_bps, maker_bps=MAKER_BPS)
                )
                result = run_backtest(
                    candles, None, strategy, config, symbol=symbol, timeframe=timeframe
                )
                metrics = _cell_metrics(result.trades, taker_bps)
                if metrics:
                    metrics.update(
                        strat=strategy_id,
                        exch=exchange,
                        sym=symbol,
                        tf=timeframe,
                        rows=len(candles),
                    )
                    rows.append(metrics)
            except Exception as exc:  # noqa: BLE001 - record and keep the grid going
                rows.append(
                    {
                        "strat": strategy_id,
                        "exch": exchange,
                        "sym": symbol,
                        "tf": timeframe,
                        "error": repr(exc)[:120],
                    }
                )
        # Flush after each strategy so partial progress is inspectable.
        out_path.write_text(json.dumps({"progress": done, "total": total, "rows": rows}))
        print(f"done {strategy_id} ({done}/{total}) elapsed={time.time() - started:.0f}s", flush=True)

    out_path.write_text(
        json.dumps(
            {
                "complete": True,
                "total": total,
                "elapsed": round(time.time() - started, 1),
                "rows": rows,
            }
        )
    )
    print(f"COMPLETE cells={len(rows)} elapsed={time.time() - started:.0f}s", flush=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent second-eye grid backtest")
    parser.add_argument("--out", default="research/live_research/second_eye_grid.json")
    args = parser.parse_args()
    run(Path(args.out))


if __name__ == "__main__":
    main()
