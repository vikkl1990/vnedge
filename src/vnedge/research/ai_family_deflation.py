"""Family-level deflation for the AI-candidate x timeframe sweep.

    python -m vnedge.research.ai_family_deflation --timeframes 30m,1h,4h

The 2026-08 indicator audits swept sandbox candidates across timeframes and
produced a small number of walk-forward CANDIDATE cells. A lone gate pass
inside a large search is exactly the situation ``ml.validation`` exists for:
this tool replays every (candidate, timeframe) cell with the same simplified
next-open executor, builds zero-filled daily net-bps series, and computes the
FAMILY statistics — per-cell Deflated Sharpe against the dispersion of every
Sharpe the search produced, and PBO across the cell matrix.

Two honesty rules are structural here:

* ``--n-trials`` must be the total number of configurations ever tried in
  this research family — including variants that were discarded before this
  run. It defaults to the number of cells evaluated NOW, which is a floor;
  the artifact records which was used.
* This is evidence about the SEARCH, not promotion evidence for any cell.
  The executor is bar-level and fee-flat; a surviving cell still owes the
  frozen pipeline its untouched-data judgment and shadow observation.

Research-only: no registry, roster, or runtime import; writes one artifact.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vnedge.ml.validation import (
    deflated_sharpe_ratio,
    effective_number_of_trials,
    probability_of_backtest_overfitting,
)
from vnedge.strategy.ai_sandbox import load_ai_strategy

#: all-in taker round trip, bps (Delta 5.9 x 2) — same figure as the slices
ROUND_TRIP_BPS = 11.8
#: holding cap per timeframe expressed in bars (~2 days)
HOLD_BARS = {"5m": 576, "15m": 192, "30m": 96, "1h": 48, "4h": 12, "1d": 2}


def replay_daily_bps(
    strategy: Any, frame: pd.DataFrame, *, hold_bars: int,
    round_trip_bps: float = ROUND_TRIP_BPS,
) -> pd.Series:
    """Zero-filled per-UTC-day net bps from a next-open, stop/target replay.

    Deliberately the same simplified executor used by the audit slices: one
    position at a time, next-open entry, stop wins ties, timeout at the cap.
    Zero-filling matters — omitting flat days inflates every Sharpe.
    """
    df = strategy.prepare(frame.copy()).reset_index(drop=True)
    open_until = -1
    daily: dict[object, float] = {}
    for i in range(int(strategy.warmup_bars), len(df) - 1):
        if i <= open_until:
            continue
        intent = strategy.signal(df, i)
        if intent is None:
            continue
        entry = float(df.iloc[i + 1]["open"])
        if entry <= 0:
            open_until = i + 1
            continue
        stop = float(intent.stop_price)
        target = (
            float(intent.take_profit_price)
            if intent.take_profit_price is not None else None
        )
        end = min(i + 1 + hold_bars, len(df) - 1)
        exit_price, exit_index = float(df.iloc[end]["close"]), end
        for j in range(i + 1, end + 1):
            bar = df.iloc[j]
            if intent.side == "long":
                if float(bar["low"]) <= stop:
                    exit_price, exit_index = min(stop, float(bar["open"])), j
                    break
                if target is not None and float(bar["high"]) >= target:
                    exit_price, exit_index = target, j
                    break
            else:
                if float(bar["high"]) >= stop:
                    exit_price, exit_index = max(stop, float(bar["open"])), j
                    break
                if target is not None and float(bar["low"]) <= target:
                    exit_price, exit_index = target, j
                    break
        direction = 1.0 if intent.side == "long" else -1.0
        net = direction * (exit_price / entry - 1.0) * 1e4 - round_trip_bps
        day = pd.Timestamp(df.iloc[i + 1]["timestamp"]).date()
        daily[day] = daily.get(day, 0.0) + net
        open_until = exit_index
    if not daily:
        return pd.Series(dtype=float)
    first = pd.Timestamp(df["timestamp"].iloc[0]).date()
    last = pd.Timestamp(df["timestamp"].iloc[-1]).date()
    index = pd.date_range(first, last, freq="D").date
    return pd.Series([daily.get(d, 0.0) for d in index], index=list(index))


def run_family_deflation(
    *,
    data_root: str = "data",
    exchange: str = "binanceusdm",
    symbol: str = "BTC/USDT:USDT",
    timeframes: tuple[str, ...] = ("30m", "1h", "4h"),
    strategy_dir: str = "data/strategies/ai",
    n_trials: float | None = None,
    n_blocks: int = 10,
) -> dict[str, Any]:
    from vnedge.data.parquet_store import ParquetStore

    store = ParquetStore(data_root)
    sources = sorted(Path(strategy_dir).glob("*.py"))
    series: dict[str, pd.Series] = {}
    skipped: list[dict[str, str]] = []
    for timeframe in timeframes:
        try:
            frame = store.read_candles(exchange, symbol, timeframe)
        except FileNotFoundError:
            skipped.append({"cell": f"*@{timeframe}", "reason": "no candles"})
            continue
        if frame is None or frame.empty:
            skipped.append({"cell": f"*@{timeframe}", "reason": "no candles"})
            continue
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        hold = HOLD_BARS.get(timeframe, 48)
        for path in sources:
            cell = f"{path.stem}@{timeframe}"
            try:
                cls = load_ai_strategy(path.read_text())
                daily = replay_daily_bps(cls(), frame, hold_bars=hold)
            except Exception as exc:  # noqa: BLE001 — a bad cell is data, not a crash
                skipped.append({"cell": cell, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if len(daily) >= 8 and (daily != 0).any():
                series[cell] = daily
            else:
                skipped.append({"cell": cell, "reason": "insufficient daily samples"})

    if len(series) < 2:
        raise RuntimeError("family deflation needs at least two evaluable cells")

    width = min(len(s) for s in series.values())
    matrix = np.column_stack([s.to_numpy()[-width:] for s in series.values()])
    sharpes: dict[str, float] = {}
    for name, s in series.items():
        values = s.to_numpy()[-width:]
        sd = float(np.std(values, ddof=1))
        sharpes[name] = float(np.mean(values) / sd) if sd > 0 else 0.0

    evaluated = len(series)
    trials = float(n_trials) if n_trials is not None else float(evaluated)
    trial_sharpes = list(sharpes.values())
    dsr = {
        name: float(
            deflated_sharpe_ratio(
                s.to_numpy()[-width:], n_trials=trials, trial_sharpes=trial_sharpes
            )
        )
        for name, s in series.items()
    }
    pbo = (
        float(probability_of_backtest_overfitting(matrix, n_blocks=n_blocks))
        if width >= n_blocks * 2 else float("nan")
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "exchange": exchange,
        "symbol": symbol,
        "timeframes": list(timeframes),
        "executor": "bar_next_open_fee_flat_v1",
        "round_trip_bps": ROUND_TRIP_BPS,
        "days_per_cell": int(width),
        "cells_evaluated": evaluated,
        "cells_skipped": skipped,
        "n_trials_used": trials,
        "n_trials_is_floor": n_trials is None,
        "effective_trials": float(effective_number_of_trials(matrix)),
        "pbo": pbo,
        "sharpe_by_cell": dict(sorted(sharpes.items(), key=lambda kv: -kv[1])),
        "dsr_by_cell": dict(sorted(dsr.items(), key=lambda kv: -kv[1])),
        "note": (
            "Family evidence about the search, not promotion evidence for any "
            "cell. DSR > 0.95 is the conventional survives-deflation line."
        ),
    }


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, allow_nan=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--exchange", default="binanceusdm")
    parser.add_argument("--symbol", default="BTC/USDT:USDT")
    parser.add_argument("--timeframes", default="30m,1h,4h")
    parser.add_argument("--strategy-dir", default="data/strategies/ai")
    parser.add_argument(
        "--n-trials", type=float, default=None,
        help="HONEST total configurations ever tried in this family "
             "(defaults to cells evaluated now, which is a floor)",
    )
    parser.add_argument(
        "--out", type=Path,
        default=Path("research/live_research/ai_family_deflation_latest.json"),
    )
    args = parser.parse_args(argv)
    payload = run_family_deflation(
        data_root=args.data_root, exchange=args.exchange, symbol=args.symbol,
        timeframes=tuple(t.strip() for t in args.timeframes.split(",") if t.strip()),
        strategy_dir=args.strategy_dir, n_trials=args.n_trials,
    )
    atomic_write(args.out, payload)
    print(f"cells={payload['cells_evaluated']} days={payload['days_per_cell']} "
          f"n_trials={payload['n_trials_used']}"
          f"{' (floor)' if payload['n_trials_is_floor'] else ''} "
          f"effective={payload['effective_trials']:.1f} pbo={payload['pbo']:.3f}")
    for name, value in list(payload["dsr_by_cell"].items())[:10]:
        print(f"  DSR {value:6.3f}  sharpe {payload['sharpe_by_cell'][name]:6.3f}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
