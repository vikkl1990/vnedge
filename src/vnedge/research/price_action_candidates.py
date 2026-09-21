"""Three causal research scanners and an isolated event replay; no order authority."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from vnedge.research.archive_scanner_screen import aggregate_archive, features, sha

CANDIDATES = ("sweep_reclaim_v1", "session_vwap_bounce_v1", "consolidation_breakout_v1")
COSTS = {"gross": (0., 0.), "base": (5.9, 3.), "stress": (5.9, 6.)}
STEP = pd.Timedelta(minutes=5)


def aggregate(minutes: pd.DataFrame) -> pd.DataFrame:
    """Keep trade notional: close*volume is not session VWAP."""
    g = minutes.resample("5min")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last",
                 "volume": "sum", "notional": "sum"})
    out["eligible"] = g.size().eq(5)
    return out


def scan(bars: pd.DataFrame) -> pd.DataFrame:
    if (not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None
            or not bars.index.is_unique or not bars.index.is_monotonic_increasing
            or not bars.index.to_series().diff().dropna().eq(STEP).all()):
        raise ValueError("unique ordered timezone-aware 5m calendar required")
    bars = bars.copy()
    bars.index = bars.index.tz_convert("UTC")
    required = ["open", "high", "low", "close", "volume", "notional"]
    observed_values = bars.loc[bars.eligible, required].to_numpy(dtype=float)
    if (not np.isfinite(observed_values).all() or (observed_values <= 0).any()
            or not bars.index.equals(bars.index.floor("5min"))):
        raise ValueError("invalid price/volume evidence or bar alignment")
    b = features(bars)
    day = b.index.tz_convert("UTC").floor("D")
    elapsed = (b.index - day) / STEP + 1
    observed = b.eligible.groupby(day).cumsum()
    b["session_complete"] = observed.to_numpy() == elapsed
    b["session_vwap"] = (
        b.notional.groupby(day).cumsum() / b.volume.groupby(day).cumsum().replace(0, np.nan)
    ).where(b.session_complete)
    for name in CANDIDATES:
        b[name] = 0
    for _, p in b[b.eligible].groupby("segment"):
        atr, vwap = p.atr, p.session_vwap
        span = (p.high - p.low).replace(0, np.nan)
        valid = p.warm & p.eligible
        rules: dict[str, list[pd.Series]] = {name: [] for name in CANDIDATES}
        for side in (1, -1):
            directional = side * (p.close - p.open) > 0
            sweep = ((p.low < p.lo) & (p.close > p.lo) if side == 1 else
                     (p.high > p.hi) & (p.close < p.hi)) & directional
            confirm = p.close > p.high.shift() if side == 1 else p.close < p.low.shift()
            rules[CANDIDATES[0]].append(sweep.shift(fill_value=False) & confirm & directional)
            above = (side * (p.close - vwap) > 0) & vwap.notna()
            held = above.shift().rolling(3).sum().eq(3)
            held &= (p.index - p.index.floor("D")) >= 3 * STEP
            touch = ((p.low <= vwap + .2 * atr) & (p.low >= vwap - .5 * atr)
                     if side == 1 else
                     (p.high >= vwap - .2 * atr) & (p.high <= vwap + .5 * atr))
            location = ((p.close - p.low) / span >= .75 if side == 1 else
                        (p.high - p.close) / span >= .75)
            rules[CANDIDATES[1]].append(
                held & touch & above & directional & location
                & ((p.close - p.open).abs() / span >= .5)
                & (p.volume >= 1.2 * p.volbase) & (p.volume.shift() <= p.volbase)
            )
            breakout = p.close > p.hi if side == 1 else p.close < p.lo
            rules[CANDIDATES[2]].append(
                (p.hi - p.lo <= 4 * p.prior_atr) & breakout & (p.volume >= 1.5 * p.volbase)
            )
        for name, (long, short) in rules.items():
            b.loc[p.index, name] = np.select(
                [long & ~short & valid, short & ~long & valid], [1, -1], default=0,
            )
    return b


def replay(b: pd.DataFrame, candidate: str, *, fee_bps: float, slip_bps: float
           ) -> tuple[pd.DataFrame, dict[str, int]]:
    if candidate not in CANDIDATES or fee_bps < 0 or slip_bps < 0:
        raise ValueError("invalid replay contract")
    records = []
    busy = -1
    counts = {"candidates": 0, "overlap": 0, "censored": 0, "entry_distance": 0}
    for i in np.flatnonzero(b[candidate].to_numpy()):
        counts["candidates"] += 1
        if i < busy:
            counts["overlap"] += 1
            continue
        side = int(b.iloc[i][candidate])
        # Reserve horizon on missing evidence; never invent a gap liquidation.
        if i + 12 >= len(b):
            counts["censored"] += 1
            busy = i + 12
            continue
        atr = float(b.atr.iloc[i])
        entry = float(b.open.iloc[i + 1]) * (1 + side * slip_bps / 10000)
        if not np.isfinite([atr, entry]).all() or atr <= 0 or entry <= 0:
            counts["censored"] += 1
            busy = i + 12
            continue
        if candidate == CANDIDATES[1] and abs(entry - b.session_vwap.iloc[i]) > atr:
            counts["entry_distance"] += 1
            continue
        stop, target = entry - side * 1.5 * atr, entry + side * 3 * atr
        for j in range(i + 1, i + 13):
            row = b.iloc[j]
            if not row.eligible:
                counts["censored"] += 1
                busy = i + 12
                break
            hit_stop = row.low <= stop if side == 1 else row.high >= stop
            hit_target = row.high >= target if side == 1 else row.low <= target
            if hit_stop:
                raw_exit = min(stop, row.open) if side == 1 else max(stop, row.open)
                reason = "stop"
            elif hit_target:
                raw_exit, reason = target, "target"
            elif j == i + 12:
                raw_exit, reason = row.close, "timeout"
            else:
                continue
            exit_price = raw_exit * (1 - side * slip_bps / 10000)
            gross = side * (exit_price - entry) / entry * 10000
            fees = fee_bps * (1 + exit_price / entry)
            records.append({
                "decision_close": (b.index[i] + STEP).isoformat(), "entry_time": b.index[i+1].isoformat(),
                "exit_bar": b.index[j].isoformat(), "side": side, "entry": entry, "exit": exit_price,
                "stop": stop, "target": target, "reason": reason, "gross_bps": gross, "net_bps": gross-fees,
                "block": b.index[i+1].hour // 6,
            })
            busy = j
            break
    return pd.DataFrame(records), counts


def summarize(trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return {"trades": 0}
    net = trades.net_bps.to_numpy()
    curve = np.r_[0., np.cumsum(net)]
    loss = -net[net < 0].sum()
    return {"trades": len(net), "mean_gross_bps": float(trades.gross_bps.mean()),
                "mean_net_bps": float(net.mean()), "win_fraction": float((net > 0).mean()),
                "profit_factor": float(net[net > 0].sum()/loss) if loss else None,
                "additive_drawdown_bps": float((np.maximum.accumulate(curve)-curve).max())}


def run(downloads: Path, output: Path, contract: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "CONTRACT.md").write_bytes(contract.read_bytes())
    report = {"can_trade": False, "can_promote": False, "performance_eligible": False,
                  "exploratory": True, "funding_included": False, "timezone": "UTC_ASSUMED",
                  "contract_sha256": sha(contract), "code_sha256": sha(Path(__file__)),
                  "aggregation_code_sha256": sha(Path(__file__).with_name("archive_scanner_screen.py")),
                  "inputs": [], "results": []}
    for symbol in ("BTCUSD", "ETHUSD"):
        path = downloads / f"futures-trades-monthly-{symbol}-2026-08.csv.zip"
        minutes, audit = aggregate_archive(path, symbol)
        b = scan(aggregate(minutes))
        b.to_parquet(output / f"{symbol}_features.parquet")
        audit.update(calendar_bars=len(b), eligible_bars=int(b.eligible.sum()),
                     vwap_covered_bars=int(b.session_complete.sum()))
        report["inputs"].append(audit)
        for name in CANDIDATES:
            for cost, (fee, slip) in COSTS.items():
                trades, counts = replay(b, name, fee_bps=fee, slip_bps=slip)
                trades.to_json(output / f"{symbol}_{name}_{cost}.jsonl", orient="records", lines=True)
                result = {"symbol": symbol, "candidate": name, "cost": cost, "counts": counts,
                              "metrics": summarize(trades), "by_side": {}, "by_block": {}}
                if not trades.empty:
                    for group, column in (("by_side", "side"), ("by_block", "block")):
                        result[group] = {str(k): summarize(v) for k, v in trades.groupby(column)}
                report["results"].append(result)
                print(symbol, name, cost, result["metrics"], flush=True)
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--downloads", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    run(args.downloads, args.output, args.contract)
