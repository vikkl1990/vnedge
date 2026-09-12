"""Exploratory frozen-scanner replay; no registry, venue or capital writes.

Input is a read-only VM export. Official HTF cache provenance is explicit;
decision candles must carry canonical source, closed proof and a stored hash.
This is mechanism evidence, not live approval parity or an OOS judgment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

import pandas as pd

from vnedge.research.scanner_evidence import replay_scanner
from vnedge.runtime.multi_lane import _overlay_canonical_history

START = pd.Timestamp("2026-09-01T00:00:00Z")
END = pd.Timestamp("2026-09-11T00:00:00Z")


def lake(root: Path, symbol: str, tf: str) -> tuple[pd.DataFrame, list[dict]]:
    frames, manifest = [], []
    for path in sorted((root / symbol / tf).glob("*.parquet")):
        frame = pd.read_parquet(path)
        timestamps = pd.to_datetime(frame["open_time"], utc=True)
        selected = frame.loc[timestamps.ge(START) & timestamps.lt(END)].copy()
        if selected.empty:
            continue
        required = {"source", "content_sha256", "is_closed", "data_quality"}
        if not required.issubset(selected):
            raise ValueError(f"Missing provenance: {path}")
        if not selected["source"].eq("canonical_tick_lake").all():
            raise ValueError(f"Noncanonical lake source: {path}")
        if not selected["is_closed"].eq(True).all():
            raise ValueError(f"Unclosed lake row: {path}")
        if not selected["content_sha256"].astype(str).str.fullmatch(r"[0-9a-f]{64}").all():
            raise ValueError(f"Missing stored hash: {path}")
        manifest.append(
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
        selected = selected.rename(columns={"open_time": "timestamp", "source": "candle_source"})
        selected["timestamp"] = pd.to_datetime(selected["timestamp"], utc=True)
        selected["symbol"], selected["timeframe"] = symbol, tf
        for col in (
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "vwap",
            "taker_buy_volume",
        ):
            if col in selected:
                selected[col] = pd.to_numeric(selected[col], errors="raise").astype(float)
        frames.append(selected)
    result = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    if result["timestamp"].duplicated().any():
        raise ValueError("Duplicate closed-bar identities")
    return result, manifest


def run(root: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for symbol in ("BTCUSD", "ETHUSD"):
        frame, manifest = lake(root, symbol, "15m")
        frame = frame.loc[frame.timestamp.ge(START)].reset_index(drop=True)
        contexts, sources = {}, {}
        for tf in ("4h", "1d"):
            paths = list(root.glob(f"*__{symbol.lower()}_*.context_{tf}.candles.parquet"))
            if len(paths) != 1:
                raise ValueError(f"Expected one official {symbol}/{tf} cache")
            cache = paths[0]
            official = pd.read_parquet(cache)
            official["timestamp"] = pd.to_datetime(official.timestamp, utc=True)
            exact, hashes = lake(root, symbol, tf)
            # Reuse runtime source binding. Union timestamps first because its
            # overlay deliberately updates only rows in the supplied history.
            columns = ["timestamp", "open", "high", "low", "close", "volume"]
            history = pd.concat([official[columns], exact[columns]]).drop_duplicates(
                "timestamp", keep="last"
            )
            contexts[tf] = _overlay_canonical_history(
                history.sort_values("timestamp"),
                exact,
                allow_validated_exchange_ohlcv=True,
                timeframe=tf,
                symbol=symbol,
            )
            sources[tf] = contexts[tf].candle_source.value_counts().to_dict()
            manifest += hashes + [
                {
                    "path": str(cache),
                    "sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
                    "source": "official_delta_ohlcv_cache",
                }
            ]
        strategy_id = f"htf_regime_continuation_15m_v2__{symbol}"
        report = replay_scanner(
            strategy_id, frame, exchange_id="delta_india", context_candles=contexts
        )
        records = report.pop("records")
        report["regime_reasons"] = dict(
            Counter(r["features"].get("regime_reason") for r in records)
        )
        report["structure_reasons"] = dict(
            Counter(r["features"].get("bos15_structure_health_reason") for r in records)
        )
        report["input"] = {
            "start": START.isoformat(),
            "end_exclusive": END.isoformat(),
            "first_bar": str(frame.timestamp.min()),
            "last_bar": str(frame.timestamp.max()),
            "expected_slots": 960,
            "stored_rows": len(frame),
            "missing_opens": [
                str(t)
                for t in pd.date_range(START, END, freq="15min", inclusive="left").difference(
                    frame.timestamp
                )
            ],
            "quality_counts": frame.data_quality.value_counts().to_dict(),
            "htf_source_counts": sources,
            "files": manifest,
        }
        report["evidence_limitations"] = [
            "Exploratory already-seen window; not untouched OOS.",
            "Existing scanner replay applies explicit-quality warmup quarantine; not lane-by-lane live transport parity.",
            "Replay does not execute live target admission, CostGate, sizing or kernel submission.",
            "Replay outcome loop does not reproduce every strategy/trailing exit; nonzero outcomes are not execution proof.",
            "No repaired bars, loosened gates, new edge estimates or strategy changes.",
            "Zero trades mean economics unmeasured, not zero-risk or successful parity.",
        ]
        report["approval_parity_eligible"] = False
        report["source_revision"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        report["economics_verdict"] = (
            "UNMEASURED_ZERO_TRADES" if report["trades"] == 0 else "EXPLORATORY_ONLY"
        )
        report["can_trade"] = False
        report["can_promote"] = False
        (output / f"{symbol}_replay.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n"
        )
        print(
            json.dumps(
                {
                    k: report.get(k)
                    for k in (
                        "strategy_id",
                        "bars",
                        "evaluations",
                        "signals",
                        "trades",
                        "failed_gates",
                        "regime_reasons",
                        "structure_reasons",
                        "performance_eligible",
                    )
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.input, args.output)
