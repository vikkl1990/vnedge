"""Audit frozen replay artifacts without changing or rerunning the strategy."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from vnedge.execution.evidence import DecisionEnvelope

HERE = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encoded_sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify(root: Path) -> dict:
    result = json.loads((root / "results.json").read_text())
    for path, digest in result["source_hashes"].items():
        assert sha(Path(path)) == digest, path
    assert not result["can_trade"] and not result["can_promote"] and not result["performance_eligible"]
    summary = {}
    for symbol, data in result["symbols"].items():
        for item in data["decision_inputs"] + data["minute_inputs"]["files"]:
            assert sha(Path(item["path"])) == item["sha256"]
        rows = [json.loads(line) for line in (root / f"{symbol}.evaluations.jsonl").open()]
        assert len(rows) == data["counts"]["evaluations"]
        assert len(rows) + data["counts"]["missing_decisions"] == 2880
        candidates = {r["candidate"]["signal"]["decision_envelope"]["decision_id"]: r["candidate"]
                      for r in rows if "candidate" in r}
        assert len(candidates) == data["counts"].get("candidates", 0)
        reasons = Counter(r["diagnostics"]["primary_failed_gate"] for r in rows
                          if r["diagnostics"]["primary_failed_gate"])
        assert reasons == data["primary_failed_gates"]
        for candidate in candidates.values():
            signal = candidate["signal"]
            envelope = DecisionEnvelope.from_dict(signal["decision_envelope"])
            assert envelope.permission_snapshot.context_bars == ()
            assert signal["expected_gross_edge_bps"] is None
            assert not candidate["can_trade"] and not candidate["performance_eligible"]
            assert candidate["path_id"] == "research_observe"
            assert candidate["spec_sha256"] == encoded_sha(result["spec"])
            assert candidate["evidence_id"] == encoded_sha(
                (candidate["spec_sha256"], symbol, signal["side"], candidate["input_bar_hashes"]))
        net, exit_counts = [], Counter()
        previous_reserved = None
        for event in data["events"]:
            candidate = candidates[event["decision_id"]]
            assert event["episode_id"] == candidate["episode_id"]
            assert event["evidence_id"] == candidate["evidence_id"]
            assert event["stop_price"] == candidate["signal"]["stop_price"]
            assert event["target_price"] == candidate["signal"]["take_profit_price"]
            entry = pd.Timestamp(event["entry_time"])
            assert entry == pd.Timestamp(event["decision_close"])
            assert entry - pd.Timestamp(event["decision_open"]) == pd.Timedelta(minutes=5)
            if previous_reserved is not None:
                assert entry >= previous_reserved
            previous_reserved = pd.Timestamp(event["reserved_until"])
            if event["status"] == "measured":
                assert entry <= previous_reserved <= entry + pd.Timedelta(minutes=15)
                side = 1 if event["side"] == "long" else -1
                gross = side * (event["exit_proxy"] / event["entry_proxy"] - 1) * 10000
                assert np.isclose(event["gross_bps"], gross)
                assert np.isclose(event["net_bps"], gross - 17.8)
                net.append(event["net_bps"])
                exit_counts[event["exit_reason"]] += 1
                if event["exit_reason"] == "timeout":
                    assert previous_reserved == entry + pd.Timedelta(minutes=15)
        assert len(net) == data["summary"]["n"]
        if net:
            assert np.isclose(np.mean(net), data["summary"]["net_mean_bps"])
        assert exit_counts == data["exit_counts"]
        summary[symbol] = {"evaluations": len(rows), "candidates": len(candidates),
                           "measured": len(net), "exit_counts": exit_counts}
    return {"passed": True, "sources_verified": len(result["source_hashes"]),
            "result_sha256": sha(root / "results.json"), "symbols": summary}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt", type=Path, default=HERE / "attempt_01")
    args = parser.parse_args()
    report = verify(args.attempt)
    (args.attempt / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
