"""Fixed, offline response screen. No scanner registration or execution access."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.plan.cost_model import CostModel

START = pd.Timestamp("2026-09-01T00:00:00Z")
END = pd.Timestamp("2026-09-11T00:00:00Z")
MINUTE = pd.Timedelta(minutes=1)
HERE = Path(__file__).resolve().parent


def load_minutes(root: Path, symbol: str) -> tuple[pd.DataFrame, dict]:
    frames, manifest = [], []
    for path in sorted((root / symbol / "1m").glob("*.parquet")):
        raw = pd.read_parquet(path)
        ts = pd.to_datetime(raw.open_time, utc=True)
        selected = raw.loc[ts.ge(START) & ts.lt(END)].copy()
        if selected.empty:
            continue
        manifest.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        frames.append(selected)
    if not frames:
        raise ValueError(f"No input for {symbol}")
    frame = pd.concat(frames, ignore_index=True)
    frame["open_time"] = pd.to_datetime(frame.open_time, utc=True)
    frame["close_time"] = pd.to_datetime(frame.close_time, utc=True)
    if frame.open_time.duplicated().any():
        raise ValueError("Duplicate minute identities")
    counts: Counter = Counter()
    eligible = []
    for row in frame.to_dict("records"):
        reasons = []
        if row["open_time"] != row["open_time"].floor("min"):
            raise ValueError("Unaligned minute")
        if row["close_time"] != row["open_time"] + MINUTE:
            reasons.append("close_identity")
        if row.get("source") != "canonical_tick_lake":
            reasons.append("source")
        if row.get("is_closed") is not True:
            reasons.append("closed_proof")
        if row.get("coverage_ok") is not True or row.get("data_quality") != "ok":
            reasons.append("coverage_quality")
        expected_hash = bar_content_sha256(
            row, open_time=row["open_time"].to_pydatetime(),
            close_time=row["close_time"].to_pydatetime(), source=row.get("source", ""),
        )
        if row.get("content_sha256") != expected_hash:
            reasons.append("content_hash")
        values = np.array([float(row[k]) for k in
                           ("open", "high", "low", "close", "volume", "quote_volume")])
        if not np.isfinite(values).all() or not (values > 0).all():
            reasons.append("finite_positive")
        o, h, lo, c = values[:4]
        if not lo <= min(o, c) <= max(o, c) <= h:
            reasons.append("ohlc_geometry")
        counts.update(reasons)
        eligible.append(not reasons)
    frame["eligible"] = eligible
    frame = frame.set_index("open_time").sort_index()
    for col in ("open", "high", "low", "close", "quote_volume"):
        frame[col] = pd.to_numeric(frame[col], errors="raise").astype(float)
    stored = len(frame)
    frame = frame.reindex(pd.date_range(START, END, freq="min", inclusive="left"))
    frame["eligible"] = frame.eligible.eq(True)
    return frame, {
        "files": manifest, "stored_minutes": stored, "expected_minutes": len(frame),
        "missing_minutes": len(frame) - stored, "eligible_minutes": int(frame.eligible.sum()),
        "invalid_reason_counts": dict(counts),
    }


def five_minute_frame(minutes: pd.DataFrame) -> pd.DataFrame:
    """Only complete eligible children make an eligible research parent."""
    groups = minutes.resample("5min", label="left", closed="left")
    bars = groups.agg({"open": "first", "high": "max", "low": "min", "close": "last",
                       "quote_volume": "sum", "eligible": "sum"})
    bars["eligible"] = bars.eligible.eq(5) & groups.size().eq(5)
    # Calendar-spaced invalid rows remain in the frame, preventing gap bridging.
    prior_good = bars.eligible.shift(1).rolling(12).sum().eq(12)
    baseline = bars.quote_volume.shift(1).rolling(12).median()
    body = (bars.close / bars.open - 1) * 10000
    width = bars.high - bars.low
    near_extreme = ((body > 0) & ((bars.high - bars.close) <= 0.2 * width)) | (
        (body < 0) & ((bars.close - bars.low) <= 0.2 * width)
    )
    bars["body_bps"] = body
    bars["notional_ratio"] = bars.quote_volume / baseline
    bars["setup"] = (bars.eligible & prior_good & baseline.gt(0) & width.gt(0)
                     & bars.notional_ratio.ge(2) & body.abs().ge(10) & near_extreme)
    return bars


def events(minutes: pd.DataFrame, bars: pd.DataFrame, claim: str) -> tuple[list[dict], dict]:
    if claim not in {"continuation", "reversal"}:
        raise ValueError(claim)
    records = []
    occupied_until = bars.index.min()
    counts: Counter = Counter(setups=int(bars.setup.sum()))
    for timestamp, row in bars.loc[bars.setup].iterrows():
        decision_close = timestamp + 5 * MINUTE
        if decision_close < occupied_until:
            counts["overlapping_skipped"] += 1
            continue
        entry_time = decision_close + MINUTE
        exit_time = entry_time + 15 * MINUTE
        occupied_until = exit_time  # reserve even if future observations are censored
        required = pd.date_range(decision_close, exit_time, freq="min")
        path = minutes.reindex(required)
        side = (1 if row.body_bps > 0 else -1) * (1 if claim == "continuation" else -1)
        record = {
            "decision_open": timestamp.isoformat(), "decision_close": decision_close.isoformat(),
            "entry_time": entry_time.isoformat(), "exit_time": exit_time.isoformat(),
            "side": side, "body_bps": float(row.body_bps),
            "notional_ratio": float(row.notional_ratio),
        }
        if not path.eligible.eq(True).all():
            record["status"] = "censored_missing_or_invalid_future"
            counts["censored"] += 1
        else:
            entry, exit_price = float(path.loc[entry_time, "open"]), float(path.loc[exit_time, "open"])
            record.update(status="measured", entry_proxy=entry, exit_proxy=exit_price,
                          gross_bps=side * (exit_price / entry - 1) * 10000)
            counts["measured"] += 1
        records.append(record)
    return records, dict(counts)


def summarize(records: list[dict], cost: float) -> dict:
    measured = [r for r in records if r["status"] == "measured"]
    if not measured:
        return {"n": 0, "verdict": "UNMEASURED", "can_trade": False,
                "can_promote": False, "performance_eligible": False}
    gross = np.array([r["gross_bps"] for r in measured])
    net = gross - cost
    equity = np.r_[0, net.cumsum()]
    days = pd.date_range(START, END, freq="D", inclusive="left")
    day_sums, day_counts = [], []
    for day in days:
        values = [r["gross_bps"] - cost for r in measured
                  if pd.Timestamp(r["decision_open"]).floor("D") == day]
        day_sums.append(sum(values))
        day_counts.append(len(values))
    sampled = np.random.default_rng(20260912).integers(0, len(days), size=(2000, len(days)))
    sums, counts = np.asarray(day_sums)[sampled].sum(axis=1), np.asarray(day_counts)[sampled].sum(axis=1)
    boot = sums[counts > 0] / counts[counts > 0]
    blocks = {}
    for name, a, b in (("first_five_days", START, START + pd.Timedelta(days=5)),
                       ("second_five_days", START + pd.Timedelta(days=5), END)):
        values = [r["gross_bps"] - cost for r in measured if a <= pd.Timestamp(r["decision_open"]) < b]
        blocks[name] = {"n": len(values), "mean_net_bps": float(np.mean(values)) if values else None}
    losses = -net[net < 0].sum()
    verdict = "INSUFFICIENT_SAMPLE" if len(net) < 30 else (
        "UNSUPPORTED_AT_MODELED_COST" if net.mean() <= 0 else "EXPLORATORY_LEAD_ONLY"
    )
    return {
        "n": len(net), "gross_mean_bps": float(gross.mean()), "net_mean_bps": float(net.mean()),
        "net_median_bps": float(np.median(net)), "net_win_rate": float((net > 0).mean()),
        "profit_factor": float(net[net > 0].sum() / losses) if losses else None,
        "drawdown_cumulative_net_bps": float((np.maximum.accumulate(equity) - equity).max()),
        "fee_only_mean_bps": float((gross - 11.8).mean()),
        "stress_mean_bps": float((gross - 23.8).mean()),
        "best_removed_mean_bps": float((net.sum() - net.max()) / (len(net) - 1)) if len(net) > 1 else None,
        "descriptive_day_bootstrap_95_mean_bps": np.quantile(boot, [0.025, 0.975]).tolist(),
        "reporting_blocks_not_oos": blocks, "daily_net_bps": dict(zip(map(str, days), day_sums)),
        "verdict": verdict, "can_trade": False, "can_promote": False, "performance_eligible": False,
    }


def run(root: Path, output: Path) -> None:
    cost = CostModel.for_profile("delta_scalp_v2")
    booked = cost.round_trip_bps(include_safety=False)
    if not np.isclose(booked, 17.8):
        raise ValueError("Cost changed since frozen contract")
    result = {
        "contract_sha256": hashlib.sha256((HERE / "CONTRACT.md").read_bytes()).hexdigest(),
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cost_model_sha256": hashlib.sha256(Path("src/vnedge/plan/cost_model.py").read_bytes()).hexdigest(),
        "cost_profile_id": cost.profile, "cost_config_sha256": cost.config_sha256,
        "booked_round_bps": booked, "funding": "excluded", "fill_assumption": "delayed_minute_open_proxy",
        "start": START.isoformat(), "end_exclusive": END.isoformat(), "symbols": {},
        "untouched_oos": False, "can_trade": False, "can_promote": False, "performance_eligible": False,
    }
    for symbol in ("BTCUSD", "ETHUSD"):
        minutes, metadata = load_minutes(root, symbol)
        bars = five_minute_frame(minutes)
        cells = {}
        for claim in ("continuation", "reversal"):
            records, counts = events(minutes, bars, claim)
            cells[claim] = {"counts": counts, "summary": summarize(records, booked), "events": records}
        result["symbols"][symbol] = {
            "input": metadata, "eligible_5m_bars": int(bars.eligible.sum()), "cells": cells,
        }
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=HERE / "results.json")
    args = parser.parse_args()
    run(args.input, args.output)
