"""Offline, write-once candle execution replay for the frozen research ID."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import subprocess
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

import vnedge.research.range_break_retest as strategy_module
from vnedge.plan.cost_model import CostModel
from vnedge.research.range_break_retest import SPEC, RangeBreakRetestResearch, ResearchCandidate

HERE = Path(__file__).resolve().parent
START = pd.Timestamp("2026-09-01T00:00:00Z")
END = pd.Timestamp("2026-09-11T00:00:00Z")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_reference() -> Any:
    path = HERE.parent / "burst_response_20260912" / "screen.py"
    spec = importlib.util.spec_from_file_location("frozen_data_reference", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_decisions(root: Path, symbol: str) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    frames, manifest = [], []
    for path in sorted((root / symbol / "5m").glob("*.parquet")):
        raw = pd.read_parquet(path)
        times = pd.to_datetime(raw.open_time, utc=True)
        chosen = raw.loc[times.ge(START) & times.lt(END)].copy()
        if chosen.empty:
            continue
        # Same stored content, only DTO field aliases and path lookup identity.
        chosen["timestamp"] = chosen["open_time"]
        chosen["candle_source"] = chosen["source"]
        for key, expected in (("exchange", SPEC.exchange), ("symbol", symbol), ("timeframe", "5m")):
            if key in chosen and not chosen[key].eq(expected).all():
                raise ValueError(f"stored {key} contradicts lookup identity")
            chosen[key] = expected
        frames.append(chosen)
        manifest.append({"path": str(path), "sha256": digest(path)})
    if not frames:
        raise ValueError(f"no stored canonical 5m history for {symbol}")
    frame = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    if frame.timestamp.duplicated().any():
        raise ValueError("duplicate decision identities")
    if not frame.timestamp.eq(frame.timestamp.dt.floor("5min")).all():
        raise ValueError("unaligned stored decision timestamps")
    return frame, manifest


def entry_failures(candidate: ResearchCandidate, entry: float) -> tuple[str, ...]:
    if not math.isfinite(entry) or entry <= 0:
        return ("entry_price_invalid",)
    direction = 1 if candidate.signal.side == "long" else -1
    stop, target = candidate.signal.stop_price, candidate.signal.take_profit_price
    risk = direction * (entry - stop) / entry * 10000
    room = direction * (target - entry) / entry * 10000
    cost = SPEC.booked_round_bps
    failed = []
    if risk <= 0 or room <= 0:
        failed.append("entry_outside_stop_target")
    extension = direction * (entry - candidate.broken_level) / (candidate.range_high - candidate.range_low)
    if extension <= 0:
        failed.append("entry_gap_lost_level")
    if extension > SPEC.max_extension_fraction:
        failed.append("entry_gap_chase_cap")
    if room < SPEC.min_room_cost_multiple * cost:
        failed.append("entry_gap_cost_room")
    if risk <= 0 or (room - cost) / (risk + cost) < SPEC.min_net_reward_risk:
        failed.append("entry_gap_net_reward_risk")
    return tuple(failed)


def simulate(candidate: ResearchCandidate, decision_close: pd.Timestamp,
             minutes: pd.DataFrame) -> dict[str, Any]:
    entry_time = decision_close  # exact next 5m open convention
    timeout = entry_time + pd.Timedelta(minutes=5 * SPEC.max_hold_bars)
    envelope = candidate.signal.decision_envelope
    event: dict[str, Any] = {
        "decision_open": (decision_close - pd.Timedelta(minutes=5)).isoformat(),
        "decision_close": decision_close.isoformat(), "entry_time": entry_time.isoformat(),
        "decision_id": envelope.decision_id, "snapshot_id": envelope.snapshot_id,
        "evidence_id": candidate.evidence_id, "episode_id": candidate.episode_id,
        "side": candidate.signal.side, "stop_price": candidate.signal.stop_price,
        "target_price": candidate.signal.take_profit_price,
        "reserved_until": timeout.isoformat(), "quality_score": candidate.quality_score,
        "cost_profile_id": SPEC.cost_profile_id, "booked_round_bps": SPEC.booked_round_bps,
        "fill_assumption": "canonical_1m_ohlc_next_open_stop_first",
        "path_id": "research_observe", "can_trade": False,
        "can_promote": False, "performance_eligible": False,
    }
    if entry_time not in minutes.index or not minutes.loc[entry_time, "eligible"]:
        return dict(event, status="censored_missing_entry")
    entry = float(minutes.loc[entry_time, "open"])
    event["entry_proxy"] = entry
    failed = entry_failures(candidate, entry)
    if failed:
        return dict(event, status="entry_rejected", failed_gates=failed,
                    reserved_until=entry_time.isoformat())
    side = 1 if candidate.signal.side == "long" else -1
    stop, target = event["stop_price"], event["target_price"]

    def finish(price: float, at: pd.Timestamp, reason: str, timing: str) -> dict[str, Any]:
        gross = side * (price / entry - 1) * 10000
        return dict(event, status="measured", exit_proxy=price, exit_known_by=at.isoformat(),
                    exit_reason=reason, exit_timing=timing, reserved_until=at.isoformat(),
                    gross_bps=gross, net_bps=gross - SPEC.booked_round_bps)

    for ts in pd.date_range(entry_time, timeout, freq="min", inclusive="left"):
        if ts not in minutes.index or not minutes.loc[ts, "eligible"]:
            return dict(event, status="censored_missing_path", first_missing_at=ts.isoformat())
        row = minutes.loc[ts]
        op, high, low = (float(row[key]) for key in ("open", "high", "low"))
        # Open is ordered before the unknown intraminute path.
        if side * (op - stop) <= 0:
            return finish(op, ts, "stop_gap", "minute_open")
        if side * (op - target) >= 0:
            return finish(target, ts, "target_gap", "minute_open_no_improvement")
        stop_hit = low <= stop if side == 1 else high >= stop
        target_hit = high >= target if side == 1 else low <= target
        known_by = ts + pd.Timedelta(minutes=1)
        if stop_hit:
            return finish(stop, known_by, "stop_tie" if target_hit else "stop", "intraminute")
        if target_hit:
            return finish(target, known_by, "target", "intraminute")
        if known_by == timeout:
            return finish(float(row["close"]), timeout, "timeout", "minute_close")
    raise AssertionError("hold window unexpectedly empty")


def run(root: Path, output: Path) -> None:
    reference = load_reference()
    cost = CostModel.for_profile(SPEC.cost_profile_id)
    if cost.config_sha256 != SPEC.cost_config_sha256:
        raise ValueError("cost contract drift")
    sources = [Path(__file__), Path(strategy_module.__file__), HERE / "CONTRACT.md",
               Path(reference.__file__), Path("src/vnedge/data/bar_identity.py"),
               Path("src/vnedge/strategy/arm_evidence.py"),
               Path("src/vnedge/strategy/base_strategy.py"), Path("src/vnedge/plan/cost_model.py")]
    hashes = {str(p.resolve()): digest(p) for p in sources}
    output.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {
        "strategy_id": SPEC.strategy_id, "spec": asdict(SPEC), "source_hashes": hashes,
        "git_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "start": START.isoformat(), "end_exclusive": END.isoformat(),
        "untouched_oos": False, "funding": "excluded", "symbols": {},
        "can_trade": False, "can_promote": False, "performance_eligible": False,
    }
    for symbol in SPEC.symbols:
        minutes, minute_manifest = reference.load_minutes(root, symbol)
        frame, decision_manifest = load_decisions(root, symbol)
        engine = RangeBreakRetestResearch(symbol)
        counts: Counter = Counter(stored_decisions=len(frame), scheduled_decisions=2880,
                                  missing_decisions=2880-len(frame))
        primary, all_failed = Counter(), Counter()
        events, seen = [], set()
        occupied = START
        with (output / f"{symbol}.evaluations.jsonl").open("x") as journal:
            for index, row in frame.iterrows():
                evaluation = engine.evaluate(frame, index)
                counts["evaluations"] += 1
                primary.update(evaluation.failed_gates[:1])
                all_failed.update(evaluation.failed_gates)
                record = {"at": str(row.timestamp + pd.Timedelta(minutes=5)),
                          "diagnostics": evaluation.diagnostics()}
                candidate = evaluation.candidate
                if candidate is not None:
                    counts["candidates"] += 1
                    record["candidate"] = asdict(candidate)
                    now = row.timestamp + pd.Timedelta(minutes=5)
                    if candidate.episode_id in seen:
                        counts["duplicate_episode"] += 1
                        record["disposition"] = "duplicate_episode"
                    else:
                        seen.add(candidate.episode_id)
                        if now < occupied:
                            counts["overlap"] += 1
                            record["disposition"] = "overlap"
                        else:
                            event = simulate(candidate, now, minutes)
                            events.append(event)
                            occupied = pd.Timestamp(event["reserved_until"])
                            counts[event["status"]] += 1
                            record["disposition"] = event["status"]
                journal.write(json.dumps(record, default=str, allow_nan=False) + "\n")
        result["symbols"][symbol] = {
            "counts": counts, "primary_failed_gates": primary, "all_failed_gates": all_failed,
            "minute_inputs": minute_manifest, "decision_inputs": decision_manifest,
            "events": events, "summary": reference.summarize(events, SPEC.booked_round_bps),
            "exit_counts": Counter(e["exit_reason"] for e in events if e["status"] == "measured"),
        }
        print(json.dumps({"symbol": symbol, "counts": counts,
                          "summary": result["symbols"][symbol]["summary"]}), flush=True)
    if hashes != {str(p.resolve()): digest(p) for p in sources}:
        raise ValueError("source changed during frozen replay")
    (output / "results.json").write_text(json.dumps(result, indent=2, default=str, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("/tmp/vnedge-htf-recheck.PInWIi"))
    parser.add_argument("--output", type=Path, default=HERE / "attempt_01")
    args = parser.parse_args()
    run(args.input, args.output)
