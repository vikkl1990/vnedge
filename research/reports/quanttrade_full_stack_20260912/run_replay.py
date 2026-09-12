"""Run unchanged external ScalpStrategy in an offline, cold-state sandbox."""
from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent
START = pd.Timestamp("2026-09-01T00:00:00Z")
END = pd.Timestamp("2026-09-11T00:00:00Z")
REVISION = "8085fc190aab07c6ebc9a7d5b921e9ab9f10a1ea"
REAL_DATETIME = datetime
CLOCK = START.timestamp()


class ReplayDateTime(REAL_DATETIME):
    @classmethod
    def now(cls, tz=None):
        return REAL_DATETIME.fromtimestamp(CLOCK, tz)

    @classmethod
    def utcnow(cls):
        return REAL_DATETIME.fromtimestamp(CLOCK, UTC).replace(tzinfo=None)


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (REAL_DATETIME, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "value"):
        return clean(value.value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def patch_clocks(repo: Path) -> None:
    for module in list(sys.modules.values()):
        if (str(getattr(module, "__file__", "")).startswith(str(repo))
                and getattr(module, "datetime", None) is REAL_DATETIME):
            module.datetime = ReplayDateTime


def prepare_frames(minutes: pd.DataFrame, official: pd.DataFrame) -> dict:
    minutes = minutes.copy()
    minutes["volume"] = pd.to_numeric(minutes.volume, errors="raise").astype(float)
    fields = ["open", "high", "low", "close", "volume"]
    frames = {"1m": minutes.loc[minutes.eligible, fields].copy()}
    for tf, size in (("5m", 5), ("15m", 15), ("1h", 60)):
        grouped = minutes.resample(f"{size}min")
        rolled = grouped.agg({"open": "first", "high": "max", "low": "min",
                              "close": "last", "volume": "sum", "eligible": "sum"})
        frames[tf] = rolled.loc[rolled.eligible.eq(size) & grouped.size().eq(size), fields].copy()
    official = official.copy()
    official["timestamp"] = pd.to_datetime(official.timestamp, utc=True)
    if official.timestamp.duplicated().any():
        raise ValueError("Duplicate official context identities")
    frames["4h"] = official.set_index("timestamp").sort_index()[fields]
    sizes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
    for tf, frame in frames.items():
        frame[fields] = frame[fields].astype(float)
        frame["timestamp"] = frame.index
        frame["available_at"] = frame.index + pd.Timedelta(minutes=sizes[tf])
        if not frame.index.is_monotonic_increasing:
            raise ValueError("Unsorted input")
    return frames


def visible_frames(frames: dict, now: pd.Timestamp) -> dict:
    result = {}
    for tf, frame in frames.items():
        end = frame.available_at.searchsorted(now, side="right")
        result[tf] = frame.iloc[max(0, end - 5000):end].drop(columns="available_at").reset_index(drop=True)
    return result


def measured_event(signal: dict, now: pd.Timestamp, minutes: pd.DataFrame) -> dict:
    entry = now + pd.Timedelta(minutes=1)
    exit_time = entry + pd.Timedelta(minutes=15)
    result = {"decision_open": (now - pd.Timedelta(minutes=5)).isoformat(),
              "decision_close": now.isoformat(), "entry_time": entry.isoformat(),
              "exit_time": exit_time.isoformat(), "scanner": signal["metadata"].get("setup_type")}
    path = minutes.reindex(pd.date_range(now, exit_time, freq="min"))
    if not path.eligible.eq(True).all():
        return dict(result, status="censored_missing_or_invalid_future")
    a, b = float(path.loc[entry, "open"]), float(path.loc[exit_time, "open"])
    side = 1 if signal["side"] == "long" else -1
    return dict(result, status="measured", entry_proxy=a, exit_proxy=b,
                gross_bps=side * (b / a - 1) * 10000)


def run(repo: Path, lake: Path, output: Path) -> None:
    global CLOCK
    repo, output = repo.resolve(), output.resolve()
    if subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() != REVISION:
        raise ValueError("Upstream revision differs from contract")
    if (repo / "storage").exists():
        raise ValueError("Replay requires fresh empty-state checkout")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "evaluations.jsonl").exists():
        raise ValueError("Do not overwrite a completed or partial attempt")
    upstream_files = subprocess.check_output(["git", "-C", str(repo), "ls-files"], text=True).splitlines()
    upstream_hashes = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
                       for name in upstream_files if name.endswith((".py", ".yaml"))}
    spec = importlib.util.spec_from_file_location("burst_reference", HERE.parent / "burst_response_20260912/screen.py")
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    inputs, metadata, frames = {}, {}, {}
    for symbol in ("BTCUSD", "ETHUSD"):
        minutes, meta = reference.load_minutes(lake, symbol)
        context = list(lake.glob(f"*__{symbol.lower()}_*.context_4h.candles.parquet"))
        if len(context) != 1:
            raise ValueError("Need exactly one explicit official 4h cache")
        meta["official_4h"] = {"path": str(context[0]),
                               "sha256": hashlib.sha256(context[0].read_bytes()).hexdigest()}
        inputs[symbol], metadata[symbol] = minutes, meta
        frames[symbol] = prepare_frames(minutes, pd.read_parquet(context[0]))
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    sys.dont_write_bytecode = True
    network = Counter()

    def offline(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
            network[event] += 1
            raise PermissionError("Offline research: network/process launch refused")

    sys.addaudithook(offline)
    logging.basicConfig(filename=output / "upstream.log", level=logging.WARNING)
    from strategies.scalp_strategy import ScalpStrategy
    patch_clocks(repo)
    time.time = lambda: CLOCK
    config = yaml.safe_load((repo / "config/settings.yaml").read_text())
    assert config["bot"]["operating_mode"] == "paper_enforced"
    strategy = ScalpStrategy(config)
    detector = defaultdict(Counter)
    current_hits = []

    def observe(original):
        @functools.wraps(original)
        def wrapped(symbol, df, *args, **kwargs):
            key = f"{symbol}:{original.__name__.removeprefix('_scan_')}"
            detector[key]["calls"] += 1
            try:
                result = original(symbol, df, *args, **kwargs)
            except Exception as exc:
                detector[key][f"error:{type(exc).__name__}:{exc}"] += 1
                raise
            if result is not None:
                detector[key]["hits"] += 1
                current_hits.append({"scanner": result.name, "side": result.side.value,
                                     "confidence": result.confidence, "last_open": str(df.timestamp.iloc[-1])})
            return result
        return wrapped

    for name in dir(strategy):
        if name.startswith("_scan_") and callable(getattr(strategy, name)):
            setattr(strategy, name, observe(getattr(strategy, name)))
    totals, reasons, by_scanner = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    measured, emitted, timings = defaultdict(list), defaultdict(list), []
    occupied = {symbol: START for symbol in inputs}
    started = time.perf_counter()
    with (output / "evaluations.jsonl").open("x") as journal:
        for tick, now in enumerate(pd.date_range(START + pd.Timedelta(minutes=5), END, freq="5min", inclusive="both")):
            CLOCK = now.timestamp()
            patch_clocks(repo)
            for symbol, minute_input in inputs.items():
                alias = "BTC/USDT" if symbol == "BTCUSD" else "ETH/USDT"
                view = visible_frames(frames[symbol], now)
                if (view["5m"].empty or view["1m"].empty or
                    view["5m"].timestamp.iloc[-1] + pd.Timedelta(minutes=5) != now or
                    view["1m"].timestamp.iloc[-1] + pd.Timedelta(minutes=1) != now):
                    totals[symbol]["skipped_current_bar_missing"] += 1
                    continue
                current_hits.clear()
                t0 = time.perf_counter()
                try:
                    signals = strategy.analyze(alias, view)
                except Exception as exc:
                    journal.write(json.dumps({"symbol": symbol, "at": now.isoformat(), "fatal": repr(exc)}) + "\n")
                    journal.flush()
                    raise
                timings.append(time.perf_counter() - t0)
                totals[symbol]["analyze_calls"] += 1
                totals[symbol]["calls_with_detector_hits"] += bool(current_hits)
                status = strategy.last_scan_status.get(alias, {})
                reason = str(status.get("reason", "unspecified"))
                if not signals:
                    reasons[symbol][reason.split(":")[0]] += 1
                records = [clean(sig.to_dict()) for sig in signals]
                journal.write(json.dumps(clean({"symbol": symbol, "at": now, "reason": reason,
                    "hits": list(current_hits), "status": status, "signals": records,
                    "frame_rows": {tf: len(f) for tf, f in view.items()},
                    "history_gap_intervals": {tf: int((f.timestamp.diff().dropna() > pd.Timedelta(tf)).sum())
                                              for tf, f in view.items()}}), allow_nan=False) + "\n")
                for sig, record in zip(signals, records):
                    totals[symbol]["signals"] += 1
                    scanner = record["metadata"].get("setup_type", "unknown")
                    by_scanner[symbol][scanner] += 1
                    emitted[symbol].append(dict(record, replay_decision_close=now.isoformat()))
                    if not sig.is_entry:
                        totals[symbol]["pre_signals_not_benchmarked"] += 1
                        continue
                    totals[symbol]["entry_signals"] += 1
                    if now < occupied[symbol]:
                        totals[symbol]["overlapping_entry_signals"] += 1
                        continue
                    occupied[symbol] = now + pd.Timedelta(minutes=16)
                    measured[symbol].append(measured_event(record, now, minute_input))
            if tick % 144 == 0:
                journal.flush()
                print(json.dumps({"through": now.isoformat(), "counts": dict(totals),
                                  "elapsed_s": round(time.perf_counter() - started, 1)}), flush=True)
    result = {
        "upstream_revision": REVISION, "upstream_source_hashes": upstream_hashes,
        "contract_sha256": hashlib.sha256((HERE / "CONTRACT.md").read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "reference_sha256": hashlib.sha256(Path(reference.__file__).read_bytes()).hexdigest(),
        "settings": config, "data": metadata, "totals": totals, "primary_reason_counts": reasons,
        "detector_counts": detector, "signals_by_scanner": by_scanner, "signals": emitted,
        "network_refusals": network, "elapsed_s": time.perf_counter() - started,
        "analyze_p95_ms": float(np.percentile(timings, 95) * 1000), "results": {},
        "cost_profile_id": "delta_scalp_v2", "booked_round_bps": 17.8,
        "ml_model_available": False, "learned_history_available": False,
        "can_trade": False, "can_promote": False, "performance_eligible": False,
    }
    for symbol in inputs:
        result["results"][symbol] = {"summary": reference.summarize(measured[symbol], 17.8),
            "censored": sum(r["status"] != "measured" for r in measured[symbol]),
            "events": measured[symbol], "by_scanner": {}}
        for scanner in by_scanner[symbol]:
            subset = [r for r in measured[symbol] if r["scanner"] == scanner]
            result["results"][symbol]["by_scanner"][scanner] = reference.summarize(subset, 17.8)
    (output / "results.json").write_text(json.dumps(clean(result), indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--lake", type=Path, default=Path("/tmp/vnedge-htf-recheck.PInWIi"))
    parser.add_argument("--output", type=Path, default=HERE / "attempt_01")
    args = parser.parse_args()
    run(args.repo, args.lake, args.output)
