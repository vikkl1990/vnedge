"""Validate and bind an explicitly authored research contract, never infer one.

Usage: python -m vnedge.research.experiment_contract SOURCE.py --spec SPEC.json
The JSON must provide the full ExperimentSpec, including the exact source hash.
No registry/roster writes and no replacement of an existing contract.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from vnedge.research.experiment_packet import ExperimentSpec, digest, json_bytes, persist_once
from vnedge.strategy.ai_sandbox import load_ai_strategy


def bind_contract(source_path: Path, spec_path: Path) -> Path:
    source = source_path.read_bytes()
    spec = ExperimentSpec.model_validate_json(spec_path.read_bytes())
    cls = load_ai_strategy(source.decode("utf-8"))
    if spec.source_sha256 != digest(source) or spec.strategy_id != cls.strategy_id:
        raise ValueError("candidate_identity_mismatch")
    destination = source_path.with_suffix(".experiment.json")
    persist_once(destination, json_bytes(spec.model_dump(mode="json")))
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    args = parser.parse_args()
    print(bind_contract(args.source, args.spec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
