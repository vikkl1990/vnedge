"""Explicit research-only ML Lab commands. No capital, roster or live writes.

Register a JSON plan, freeze verified lane evidence, then run exactly once.
Prospective holdouts must be registered before their test window begins.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vnedge.ml.lab_pipeline import (
    LabPlan,
    freeze_dataset,
    pipeline_summary,
    read_object,
    register_plan,
    run_experiment,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("research/ml_lab"))
    subs = parser.add_subparsers(dest="command", required=True)
    register = subs.add_parser("register")
    register.add_argument("plan", type=Path)
    freeze = subs.add_parser("freeze")
    freeze.add_argument("plan_id")
    freeze.add_argument("--lane-dir", type=Path, default=Path("logs/paper_trials"))
    run = subs.add_parser("run")
    run.add_argument("plan_id")
    run.add_argument("dataset_id")
    subs.add_parser("status")
    family = subs.add_parser("register-family")
    family.add_argument("plan_ids", nargs="+")
    validate = subs.add_parser("validate-family")
    validate.add_argument("family_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            result = {
                "plan_id": register_plan(args.root, LabPlan.model_validate(read_object(args.plan)))
            }
        elif args.command == "freeze":
            result = {"dataset_id": freeze_dataset(args.root, args.plan_id, args.lane_dir)}
        elif args.command == "run":
            result = run_experiment(args.root, args.plan_id, args.dataset_id)
        elif args.command == "register-family":
            from vnedge.ml.lab_validation import register_family
            result = {"family_id": register_family(args.root, args.plan_ids)}
        elif args.command == "validate-family":
            from vnedge.ml.lab_validation import validate_family
            result = validate_family(args.root, args.family_id)
        else:
            result = pipeline_summary(args.root)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc), "can_trade": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
