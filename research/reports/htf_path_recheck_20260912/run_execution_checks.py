"""Synthetic admission regression, not historical performance or venue parity.

Uses the existing isolated proof fixture. No live strategy/registry edits.
Synthetic expectancy is NEVER persisted into an operational contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest


def main() -> None:
    repo = Path(__file__).resolve().parents[3]
    fixture = repo / "tests/test_shadow_execution_proof.py"
    spec = importlib.util.spec_from_file_location("htf_recheck_proof_fixture", fixture)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    output = []
    original = module.FixtureStrategy.signal
    for name, target, edge, expected in (
        ("targetless_missing_edge_like_htf", False, None, "no favorable target/edge hypothesis"),
        ("targetless_with_synthetic_edge", False, 100.0, "no favorable target/edge hypothesis"),
        ("target_with_missing_edge", True, None, "edge_estimate_missing"),
        ("target_with_synthetic_edge_positive_control", True, 100.0, None),
    ):
        with (
            tempfile.TemporaryDirectory(prefix="vnedge-admission-fixture-") as tmp,
            pytest.MonkeyPatch.context() as patch,
        ):

            def signal(self, df, index, *, include_target=target):
                result = original(self, df, index)
                return result if include_target else replace(result, take_profit_price=None)

            patch.setattr(module.FixtureStrategy, "signal", signal)
            proof = module.build_proof(Path(tmp), patch, edge=edge)
            result = asyncio.run(proof.session.run(max_bars=1))
            reasons = [r["payload"]["reason"] for r in proof.rows("cost_rejected")]
            assert result.signals_generated == 1
            assert result.orders_submitted == (1 if expected is None else 0)
            if expected:
                assert any(expected in reason for reason in reasons)
                assert not proof.rows("order_submitted")
            else:
                assert len(proof.rows("risk_decision")) == 1
                assert proof.rows("risk_decision")[0]["payload"]["approved"]
                assert len(proof.exchange.get_fills()) == 1
            output.append(
                {
                    "case": name,
                    "signals": result.signals_generated,
                    "orders_submitted": result.orders_submitted,
                    "simulated_fills": len(proof.exchange.get_fills()),
                    "rejections": reasons,
                    "assertions_passed": True,
                }
            )
    report = {
        "evidence_kind": "synthetic_plumbing_only",
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "registered_htf_logic_changed": False,
        "historical_signals_used": False,
        "synthetic_edge_bps_positive_control_only": 100.0,
        "performance_eligible": False,
        "can_trade": False,
        "can_promote": False,
        "cases": output,
    }
    Path(__file__).with_name("execution_checks.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
