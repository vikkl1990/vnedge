"""Offline, canonical-only paper-path diagnostic for the frozen BTC/ETH lanes.

No network clients, capital permission, strategy tuning, or synthetic signals.
Inputs are an explicit local bundle; a new output directory prevents mixing runs.
Funding/quotes remain unverified: this is a mechanics test, never an edge test.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from vnedge.execution.journal import DecisionJournal, verify_journal_chain
from vnedge.data.candles import TF_SECONDS
from vnedge.execution.order_manager import OrderManager
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.plan.cost_model import COST_PROFILES
from vnedge.risk.cost_gate import CostGate, CostProfile
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.position_sizer import SymbolLimits
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.paper_runner import PaperRunner
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.arm_evidence import assert_decision_row
from vnedge.strategy.htf_regime_continuation_15m_v2_pairs import BTC_STRATEGY_ID, ETH_STRATEGY_ID
from vnedge.strategy.strategy_registry import KILLED, get_strategy_class
from vnedge.strategy.scanner_contracts import scanner_runtime_contract


def load_verified_frame(path: Path, timeframe: str, symbol: str) -> pd.DataFrame:
    """Never repair, relabel, sort away duplicate bars, or invent provenance."""
    frame = pd.read_parquet(path)
    if frame.empty or "timestamp" not in frame:
        raise ValueError(f"{timeframe}: empty or missing timestamp")
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    if not stamps.is_monotonic_increasing or stamps.duplicated().any():
        raise ValueError(f"{timeframe}: unordered or duplicate timestamps")
    for row in frame.to_dict("records"):
        if row.get("symbol") != symbol or row.get("exchange") != "delta_india":
            raise ValueError(f"{timeframe}: market identity missing or mismatched")
        if row.get("coverage_ok") not in (True, 1):
            raise ValueError(f"{timeframe}: coverage unproven")
        assert_decision_row(row, timeframe=timeframe)
    if len(stamps) > 1 and not stamps.diff().iloc[1:].eq(pd.Timedelta(seconds=TF_SECONDS[timeframe])).all():
        raise ValueError(f"{timeframe}: incomplete consecutive window")
    return frame


def summarize_path(journal: DecisionJournal, *, report: Any, open_positions: int) -> dict[str, Any]:
    records = journal.read_all()
    counts = Counter(str(r["kind"]) for r in records)
    rejections = Counter(
        str(r["payload"].get("primary_failed_gate") or r["payload"].get("reason") or r["kind"])
        for r in records if r["kind"] in {
            "entry_evidence_rejected", "entry_clock_rejected", "cost_rejected", "sizing_rejected",
        } or (r["kind"] == "lane_eval" and not r["payload"].get("fired"))
    )
    # Necessary evidence is linked by decision id, not counts from unrelated trades.
    armed = {r["payload"].get("decision_id") for r in records if r["kind"] == "decision_armed"}
    clocks = {r["payload"].get("decision_id") for r in records if r["kind"] == "entry_clock_confirmed"}
    costs = {r["payload"].get("decision_id") for r in records if r["kind"] == "cost_approved"}
    risks = {r["payload"].get("execution_evidence", {}).get("decision_id") for r in records
             if r["kind"] == "risk_decision" and r["payload"].get("approved") is True}
    entries = [r["payload"] for r in records if r["kind"] == "order_intent"
               and r["payload"].get("intent", {}).get("reduce_only") is False]
    linked = [e for e in entries if e.get("execution_evidence", {}).get("decision_id") in
              (armed & clocks & costs & risks) - {None}
              and e.get("execution_evidence", {}).get("cost_decision", {}).get("approved") is True]
    filled = {r["payload"].get("client_order_id") for r in records
              if r["kind"] == "order_fill_sync" and r["payload"].get("filled_quantity", 0) > 0
              and r["payload"].get("fees_paid", 0) > 0}
    closed_entries = {r["payload"].get("entry_client_order_id") for r in records
                      if r["kind"] == "paper_exit" and r["payload"].get("final") is True
                      and r["payload"].get("client_order_id") in filled}
    completed = [e for e in linked if e.get("client_order_id") in filled & closed_entries]
    chain = verify_journal_chain(journal.path)
    complete = bool(completed and report.fills >= 2
                    and open_positions == 0 and report.reconciliation_mismatches == 0 and chain.ok)
    return {
        "schema_version": 1, "scope": "offline_paper_mechanics_not_edge",
        "mechanics_round_trip_observed": complete,
        "status": "MECHANICS_OBSERVED" if complete else "INCOMPLETE",
        "events": dict(counts), "rejections": dict(rejections),
        "linked_entry_intents": len(linked), "completed_round_trips": len(completed),
        "open_positions": open_positions,
        "journal_chain": asdict(chain), "run": asdict(report),
        "limitations": ["quotes and slippage are modeled, not historical BBO",
                        "funding settlement is not verified or booked by this replay",
                        "mechanical success is not promotion-grade validation"],
        "can_trade": False, "can_promote": False, "performance_eligible": False,
    }


async def replay_bundle(*, strategy_id: str, candles: Path, h4: Path, daily: Path,
                        output: Path) -> dict[str, Any]:
    if strategy_id not in {BTC_STRATEGY_ID, ETH_STRATEGY_ID} or strategy_id in KILLED:
        raise ValueError("only the frozen, non-killed BTC/ETH research lanes are supported")
    symbol = "BTC/USD:USD" if strategy_id == BTC_STRATEGY_ID else "ETH/USD:USD"
    paths = {"15m": candles, "4h": h4, "1d": daily}
    # Hash the exact supplied files before opening; never substitute a public feed.
    fingerprints = {tf: hashlib.sha256(path.read_bytes()).hexdigest() for tf, path in paths.items()}
    frames = {tf: load_verified_frame(path, tf, symbol) for tf, path in paths.items()}
    if fingerprints != {tf: hashlib.sha256(path.read_bytes()).hexdigest() for tf, path in paths.items()}:
        raise ValueError("input changed while reading")
    strategy = get_strategy_class(strategy_id)()
    if len(frames["15m"]) <= strategy.warmup_bars + 1:
        raise ValueError("decision history insufficient for warmup plus entry clock")
    for tf in ("4h", "1d"):
        strategy.bind_canonical_context(tf, frames[tf])
    cost_id = strategy.cost_profile_id
    costs = COST_PROFILES[cost_id]
    step = 0.001 if strategy_id == BTC_STRATEGY_ID else 0.01
    config = RunnerConfig(
        mode=RunnerMode.PAPER, symbol=symbol, timeframe="15m",
        execution_cost_exchange_id="delta_india", execution_cost_profile_id=cost_id,
        slippage_est_bps=costs.default_slip_entry_bps,
        limits=SymbolLimits(min_qty=step, qty_step=step, min_notional_usd=5,
                            maintenance_margin_rate=0.005),
    )
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"strategy_id": strategy_id, "input_sha256": fingerprints,
                "inputs": {k: str(v.resolve()) for k, v in paths.items()},
                "config": config.model_dump(mode="json"), "cost_model": asdict(costs),
                "authority": "offline_simulated_only", "performance_eligible": False}
    contract = scanner_runtime_contract(strategy_id)
    manifest["edge_support"] = {
        "gross_edge_bps": contract.oos_gross_edge_bps,
        "edge_model_id": contract.edge_model_id,
        "supported": contract.oos_gross_edge_bps is not None and contract.edge_model_id is not None,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    exchange = SimulatedExchange(FillModel(
        taker_fee_bps=costs.taker_fee_bps * costs.fee_gst_mult,
        maker_fee_bps=costs.maker_fee_bps * costs.fee_gst_mult,
        slippage_bps=max(costs.default_slip_entry_bps, costs.default_slip_exit_bps),
    ), config.starting_equity_usd)
    journal = DecisionJournal(output / "paper.journal.jsonl")
    gateway = PreTradeRiskGateway(config.risk, KillSwitch(kill_file=output / "KILL"))
    manager = OrderManager(gateway, journal, PaperBroker(exchange))
    runner = PaperRunner(strategy, frames["15m"], None, config, gateway=gateway,
                         order_manager=manager, exchange=exchange, journal=journal,
                         entry_cost_gate=CostGate(CostProfile(cost_id)),
                         require_canonical_truth=True, record_evaluations=True)
    report = await runner.run()
    result = summarize_path(journal, report=report, open_positions=len(exchange.get_positions()))
    result["edge_support"] = manifest["edge_support"]
    (output / "report.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", required=True, choices=[BTC_STRATEGY_ID, ETH_STRATEGY_ID])
    parser.add_argument("--candles", type=Path, required=True)
    parser.add_argument("--h4", type=Path, required=True)
    parser.add_argument("--daily", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(replay_bundle(strategy_id=args.strategy, candles=args.candles,
                                     h4=args.h4, daily=args.daily, output=args.output))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
