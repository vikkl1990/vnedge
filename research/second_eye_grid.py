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
        --use-active-exit --trail-atr-mult 3 \
        --out research/live_research/second_eye_grid.json
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from vnedge.backtest.backtester import BacktestConfig, run_backtest
from vnedge.backtest.fee_model import FeeModel
from vnedge.strategy.strategy_registry import STRATEGIES

warnings.filterwarnings("ignore")

MAKER_BPS = 2.0
DATA_ROOT = Path("data/normalized")


@dataclass(frozen=True)
class SecondEyeConfig:
    initial_equity_usd: float = 500.0
    max_holding_bars: int = 48
    use_active_exit: bool = True
    trail_atr_mult: float = 3.0
    trail_atr_window: int = 14
    min_trades: int = 20
    min_profit_factor: float = 1.5
    min_avg_net_bps: float = 25.0

    def to_backtest_config(self, *, taker_bps: float) -> BacktestConfig:
        return BacktestConfig(
            initial_equity_usd=self.initial_equity_usd,
            max_holding_bars=self.max_holding_bars,
            fees=FeeModel(taker_bps=taker_bps, maker_bps=MAKER_BPS),
            use_active_exit=self.use_active_exit,
            trail_atr_mult=self.trail_atr_mult,
            trail_atr_window=self.trail_atr_window,
        )


def _venue_taker_bps(exchange: str) -> float:
    # Bybit standard perp taker is 5.5 bps; Binance USDT-M / Delta India 5.0.
    return 5.5 if exchange == "bybit" else 5.0


def _agg(nets: list[float], bps: list[float]) -> dict:
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
        "avg_net_bps": round(float(np.mean(bps)), 4) if bps else 0.0,
    }


def _cell_metrics(trades, taker_bps: float) -> dict | None:
    if not trades:
        return None
    maker_ratio = MAKER_BPS / taker_bps
    taker_nets, maker_nets = [], []
    taker_net_bps, maker_net_bps = [], []
    for trade in trades:
        gross = trade.gross_pnl_usd
        fee = trade.fees_usd
        funding = trade.funding_usd
        notional = max(1e-9, abs(trade.quantity * trade.entry_price))
        taker_net = gross - fee + funding
        maker_net = gross - fee * maker_ratio + funding
        taker_nets.append(taker_net)
        maker_nets.append(maker_net)
        taker_net_bps.append(taker_net / notional * 1e4)
        maker_net_bps.append(maker_net / notional * 1e4)
    return {
        "n": len(trades),
        "taker": _agg(taker_nets, taker_net_bps),
        "maker": _agg(maker_nets, maker_net_bps),
    }


def _no_trade_cell_metrics() -> dict:
    return {
        "n": 0,
        "taker": _agg([], []),
        "maker": _agg([], []),
        "no_trade_sample": True,
    }


def _parse_path(path: Path) -> tuple[str, str, str]:
    parts = {}
    for segment in path.parts:
        if "=" in segment:
            key, value = segment.split("=", 1)
            parts[key] = value
    return parts.get("exchange", ""), parts.get("symbol", ""), parts.get("timeframe", "")


def _pre_registry(config: SecondEyeConfig) -> dict:
    return {
        "registry_id": "paper_only_survivor_prereg_v1",
        "decision_contract": (
            "Every registered strategy x exchange x symbol x timeframe candle lane "
            "is backtested with causal next-open entries and ActiveExitState parity. "
            "Survivors require sample, PF, and avg net bps after fees before PAPER-only "
            "forward observation. No shadow roster is produced by this registry."
        ),
        "entry_contract": "bar i close decision, bar i+1 open fill",
        "exit_contract": (
            "ActiveExitState partial TP, fee-aware breakeven, ATR trail, strategy "
            "exit_signal, daily factory force-flat, stop-wins-ties"
        ),
        "gates": {
            "min_trades": config.min_trades,
            "min_profit_factor": config.min_profit_factor,
            "min_avg_net_bps": config.min_avg_net_bps,
        },
        "backtest_config": asdict(config),
        "routes": {
            "taker": "strict route; charged venue taker bps",
            "maker": "optimistic maker upper-bound; paper forward must prove fills",
        },
    }


def _write_progress(
    out_path: Path,
    *,
    done: int,
    total: int,
    rows: list[dict],
    config: SecondEyeConfig,
    current: dict,
    started: float,
) -> None:
    out_path.write_text(json.dumps({
        "complete": False,
        "progress": done,
        "total": total,
        "elapsed": round(time.time() - started, 1),
        "current": current,
        "pre_registry": _pre_registry(config),
        "rows": rows,
    }))


def run(out_path: Path, *, config: SecondEyeConfig = SecondEyeConfig()) -> list[dict]:
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
                result = run_backtest(
                    candles,
                    None,
                    strategy,
                    config.to_backtest_config(taker_bps=taker_bps),
                    symbol=symbol,
                    timeframe=timeframe,
                )
                metrics = _cell_metrics(result.trades, taker_bps)
                metrics = metrics or _no_trade_cell_metrics()
                metrics.update(
                    strat=strategy_id,
                    exch=exchange,
                    sym=symbol,
                    tf=timeframe,
                    rows=len(candles),
                    exit_model=(
                        "active_exit"
                        if config.use_active_exit
                        else "legacy_single_stop_tp"
                    ),
                    trail_atr_mult=config.trail_atr_mult,
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
            current = {
                "strat": strategy_id,
                "exch": exchange,
                "sym": symbol,
                "tf": timeframe,
            }
            _write_progress(
                out_path,
                done=done,
                total=total,
                rows=rows,
                config=config,
                current=current,
                started=started,
            )
        print(f"done {strategy_id} ({done}/{total}) elapsed={time.time() - started:.0f}s", flush=True)

    out_path.write_text(
        json.dumps(
            {
                "complete": True,
                "total": total,
                "elapsed": round(time.time() - started, 1),
                "pre_registry": _pre_registry(config),
                "rows": rows,
            }
        )
    )
    print(f"COMPLETE cells={len(rows)} elapsed={time.time() - started:.0f}s", flush=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent second-eye grid backtest")
    parser.add_argument("--out", default="research/live_research/second_eye_grid.json")
    parser.add_argument("--initial-equity-usd", type=float, default=500.0)
    parser.add_argument("--max-holding-bars", type=int, default=48)
    parser.add_argument("--use-active-exit", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--trail-atr-mult", type=float, default=3.0)
    parser.add_argument("--trail-atr-window", type=int, default=14)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--min-profit-factor", type=float, default=1.5)
    parser.add_argument("--min-avg-net-bps", type=float, default=25.0)
    args = parser.parse_args()
    config = SecondEyeConfig(
        initial_equity_usd=args.initial_equity_usd,
        max_holding_bars=args.max_holding_bars,
        use_active_exit=args.use_active_exit,
        trail_atr_mult=args.trail_atr_mult,
        trail_atr_window=args.trail_atr_window,
        min_trades=args.min_trades,
        min_profit_factor=args.min_profit_factor,
        min_avg_net_bps=args.min_avg_net_bps,
    )
    run(Path(args.out), config=config)


if __name__ == "__main__":
    main()
