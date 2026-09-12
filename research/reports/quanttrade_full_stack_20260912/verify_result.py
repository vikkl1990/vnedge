"""Independent artifact checks; never runs scanners or changes their outputs."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent


def verify(attempt: Path, repo: Path) -> dict:
    result = json.loads((attempt / "results.json").read_text())
    assert result["contract_sha256"] == hashlib.sha256((HERE / "CONTRACT.md").read_bytes()).hexdigest()
    assert result["harness_sha256"] == hashlib.sha256((HERE / "run_replay.py").read_bytes()).hexdigest()
    reference = HERE.parent / "burst_response_20260912" / "screen.py"
    assert result["reference_sha256"] == hashlib.sha256(reference.read_bytes()).hexdigest()
    for name, expected in result["upstream_source_hashes"].items():
        assert hashlib.sha256((repo / name).read_bytes()).hexdigest() == expected, name
    evaluations, signals, last_times = Counter(), Counter(), {}
    statuses, gap_calls = defaultdict(Counter), Counter()
    recorded_signals = defaultdict(list)
    for line in (attempt / "evaluations.jsonl").open():
        row = json.loads(line)
        assert "fatal" not in row
        symbol = row["symbol"]
        ts = pd.Timestamp(row["at"])
        if symbol in last_times:
            assert ts > last_times[symbol]
        last_times[symbol] = ts
        evaluations[symbol] += 1
        signals[symbol] += len(row["signals"])
        gap_calls[symbol] += any(row["history_gap_intervals"].values())
        for sig in row["signals"]:
            recorded_signals[symbol].append(dict(sig, replay_decision_close=row["at"]))
            statuses[symbol][sig["metadata"].get("ml_verdict", "unknown")] += 1
    for symbol, total in result["totals"].items():
        assert evaluations[symbol] == total["analyze_calls"]
        assert signals[symbol] == total.get("signals", 0)
        assert total["analyze_calls"] + total.get("skipped_current_bar_missing", 0) == 2880
        assert len(result["signals"].get(symbol, [])) == signals[symbol]
        assert result["signals"].get(symbol, []) == recorded_signals[symbol]
        assert total.get("entry_signals", 0) + total.get("pre_signals_not_benchmarked", 0) == signals[symbol]
        previous_exit = None
        measured = []
        for event in result["results"][symbol]["events"]:
            decision = pd.Timestamp(event["decision_close"])
            entry, exit_time = pd.Timestamp(event["entry_time"]), pd.Timestamp(event["exit_time"])
            assert entry - decision == pd.Timedelta(minutes=1)
            assert exit_time - entry == pd.Timedelta(minutes=15)
            if previous_exit is not None:
                assert decision >= previous_exit
            previous_exit = exit_time
            if event["status"] == "measured":
                measured.append(event["gross_bps"] - 17.8)
        summary = result["results"][symbol]["summary"]
        assert len(result["results"][symbol]["events"]) + total.get("overlapping_entry_signals", 0) == total.get("entry_signals", 0)
        assert len(measured) == summary["n"]
        if measured:
            assert np.isclose(np.mean(measured), summary["net_mean_bps"])
            assert np.isclose(sum(measured), sum(summary["daily_net_bps"].values()))
    assert not result["can_trade"] and not result["can_promote"] and not result["performance_eligible"]
    return {
        "checks_passed": True, "upstream_files_verified": len(result["upstream_source_hashes"]),
        "evaluations": dict(evaluations), "signals": dict(signals),
        "ml_verdicts": dict(statuses), "calls_with_history_gaps": dict(gap_calls),
        "artifact_sha256": hashlib.sha256((attempt / "results.json").read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "dependencies": {name: importlib.metadata.version(name)
                         for name in ("pandas", "numpy", "pydantic", "PyYAML", "requests")},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--attempt", type=Path, default=HERE / "attempt_01")
    args = parser.parse_args()
    audit = verify(args.attempt, args.repo)
    (args.attempt / "verification.json").write_text(json.dumps(audit, indent=2) + "\n")
    print(json.dumps(audit, indent=2))
