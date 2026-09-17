"""Offline, source-verified paper-path diagnostic for the frozen BTC/ETH lanes.

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
from vnedge.data.symbols import canonical_symbol
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
from vnedge.strategy.arm_evidence import (
    TRUSTED_PERMISSION_CANDLE_SOURCES, assert_context_row, assert_decision_row,
    last_eligible_context_bar,
)
from vnedge.strategy.htf_regime_continuation_15m_v2_pairs import BTC_STRATEGY_ID, ETH_STRATEGY_ID
from vnedge.strategy.strategy_registry import KILLED, get_strategy_class
from vnedge.strategy.scanner_contracts import scanner_runtime_contract


SOURCE_POLICIES = ("canonical_only", "registered_context_v1")


def source_policy_contract(strategy_id: str, profile: str) -> dict[str, Any]:
    if strategy_id not in {BTC_STRATEGY_ID, ETH_STRATEGY_ID} or strategy_id in KILLED:
        raise ValueError("only the frozen, non-killed BTC/ETH research lanes are supported")
    if profile not in SOURCE_POLICIES:
        raise ValueError("unknown replay source policy")
    contract = scanner_runtime_contract(strategy_id)
    strategy = get_strategy_class(strategy_id)()
    if tuple(strategy.permission_context_sources) != contract.context_candle_sources:
        raise ValueError("strategy and registered context source policy disagree")
    payload = asdict(contract)
    return {
        "profile": profile, "strategy_id": strategy_id,
        "decision_sources": sorted(TRUSTED_PERMISSION_CANDLE_SOURCES),
        "context_sources": (list(contract.context_candle_sources)
                            if profile == "registered_context_v1"
                            else sorted(TRUSTED_PERMISSION_CANDLE_SOURCES)),
        "registration": payload,
        "registration_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "claim": "source_policy_only_not_execution_or_feature_parity",
    }


def load_verified_frame(path: Path, timeframe: str, symbol: str, *,
                        strategy_id: str | None = None,
                        source_policy: str = "canonical_only") -> pd.DataFrame:
    """Never repair, relabel, sort away duplicate bars, or invent provenance."""
    if timeframe not in {"15m", "4h", "1d"}:
        raise ValueError("unsupported replay timeframe")
    policy = (source_policy_contract(strategy_id, source_policy) if strategy_id else None)
    if policy is None and source_policy != "canonical_only":
        raise ValueError("registered context requires a strategy registration")
    if strategy_id and symbol != ("BTC/USD:USD" if strategy_id == BTC_STRATEGY_ID else "ETH/USD:USD"):
        raise ValueError("registration market mismatch")
    frame = pd.read_parquet(path)
    if frame.empty or "timestamp" not in frame:
        raise ValueError(f"{timeframe}: empty or missing timestamp")
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    if stamps.isna().any() or not stamps.is_monotonic_increasing or stamps.duplicated().any():
        raise ValueError(f"{timeframe}: unordered or duplicate timestamps")
    for row in frame.to_dict("records"):
        if row.get("symbol") not in {symbol, canonical_symbol(symbol)} or row.get("exchange") != "delta_india":
            raise ValueError(f"{timeframe}: market identity missing or mismatched")
        if "timeframe" in row and row["timeframe"] != timeframe:
            raise ValueError(f"{timeframe}: timeframe identity mismatch")
        for flag in ("coverage_ok", "is_closed"):
            value = row.get(flag)
            if pd.isna(value) or isinstance(value, str) or value not in (True, 1):
                raise ValueError(f"{timeframe}: {flag} unproven")
        if "source" in row and row["source"] != row.get("candle_source"):
            raise ValueError(f"{timeframe}: conflicting source identity")
        if "open_time" in row and pd.Timestamp(row["open_time"]) != pd.Timestamp(row["timestamp"]):
            raise ValueError(f"{timeframe}: conflicting open time")
        if timeframe != "15m" and policy is not None:
            assert_context_row(row, timeframe=timeframe, allowed_sources=policy["context_sources"])
        else:
            assert_decision_row(row, timeframe=timeframe)
    if len(stamps) > 1 and not stamps.diff().iloc[1:].eq(pd.Timedelta(seconds=TF_SECONDS[timeframe])).all():
        raise ValueError(f"{timeframe}: incomplete consecutive window")
    return frame


def _read_bundle(strategy_id: str, paths: dict[str, Path], profile: str
                 ) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    policy = source_policy_contract(strategy_id, profile)
    symbol = "BTC/USD:USD" if strategy_id == BTC_STRATEGY_ID else "ETH/USD:USD"
    frames: dict[str, pd.DataFrame] = {}
    fingerprints: dict[str, str] = {}
    issues: list[dict[str, str]] = []
    inventory: dict[str, Any] = {}
    for tf, path in paths.items():
        try:
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            fingerprints[tf] = before
            frame = load_verified_frame(path, tf, symbol, strategy_id=strategy_id, source_policy=profile)
            after = hashlib.sha256(path.read_bytes()).hexdigest()
            if before != after:
                raise ValueError("input changed while reading")
            frames[tf], fingerprints[tf] = frame, before
            inventory[tf] = {"rows": len(frame), "first_open": str(frame.timestamp.iloc[0]),
                             "last_open": str(frame.timestamp.iloc[-1]),
                             "source_counts": frame.candle_source.value_counts().to_dict()}
        except (OSError, ValueError, TypeError) as exc:
            issues.append({"timeframe": tf, "reason": str(exc)})
    # A second check across the entire bundle catches replacement of an earlier
    # file while another timeframe was read. No frames reach replay on failure.
    for tf, digest in fingerprints.items():
        try:
            if hashlib.sha256(paths[tf].read_bytes()).hexdigest() != digest:
                raise ValueError("input changed while reading bundle")
        except (OSError, ValueError) as exc:
            issues.append({"timeframe": tf, "reason": str(exc)})
    bindings: dict[str, Any] = {}
    if not issues:
        decision = frames["15m"]
        for label, index in (("first", 0), ("last", len(decision) - 1)):
            close = pd.Timestamp(decision.timestamp.iloc[index]) + pd.Timedelta(minutes=15)
            bindings[label] = {}
            for tf in ("4h", "1d"):
                ref = last_eligible_context_bar(frames[tf], timeframe=tf,
                    decision_close=close.to_pydatetime(), allowed_sources=policy["context_sources"])
                bindings[label][tf] = ref.as_dict() if ref else None
    report = {
        "schema_version": 1, "scope": "offline_input_preflight",
        "status": "INPUTS_VERIFIED" if not issues else "INPUTS_REJECTED",
        "source_policy": policy, "input_sha256": fingerprints, "inventory": inventory,
        "inputs": {tf: str(path.resolve()) for tf, path in paths.items()},
        "issues": issues, "asof_boundary_examples": bindings,
        "limitations": ["input admission does not establish sufficient indicator warmup",
                        "hash integrity does not independently authenticate the original supplier",
                        "this does not prove forward/replay feature or execution parity"],
        "can_trade": False, "can_promote": False, "performance_eligible": False,
    }
    return frames, report


def preflight_bundle(*, strategy_id: str, candles: Path, h4: Path, daily: Path,
                     source_policy: str = "canonical_only") -> dict[str, Any]:
    """Read-only admission: report every invalid input, write nothing, no venue."""
    return _read_bundle(strategy_id, {"15m": candles, "4h": h4, "1d": daily}, source_policy)[1]


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
                        output: Path, source_policy: str = "canonical_only") -> dict[str, Any]:
    symbol = "BTC/USD:USD" if strategy_id == BTC_STRATEGY_ID else "ETH/USD:USD"
    paths = {"15m": candles, "4h": h4, "1d": daily}
    frames, preflight = _read_bundle(strategy_id, paths, source_policy)
    if preflight["issues"]:
        raise ValueError("replay inputs rejected: " + json.dumps(preflight["issues"]))
    fingerprints = preflight["input_sha256"]
    strategy = get_strategy_class(strategy_id)()
    if len(frames["15m"]) <= strategy.warmup_bars + 1:
        raise ValueError("decision history insufficient for warmup plus entry clock")
    for tf in ("4h", "1d"):
        strategy.bind_canonical_context(tf, frames[tf])
    contract = scanner_runtime_contract(strategy_id)
    cost_id = strategy.cost_profile_id
    costs = COST_PROFILES[cost_id]
    step = 0.001 if strategy_id == BTC_STRATEGY_ID else 0.01
    config = RunnerConfig(
        mode=RunnerMode.PAPER, symbol=symbol, timeframe="15m",
        max_holding_bars=contract.max_holding_bars,
        execution_cost_exchange_id="delta_india", execution_cost_profile_id=cost_id,
        slippage_est_bps=costs.default_slip_entry_bps,
        limits=SymbolLimits(min_qty=step, qty_step=step, min_notional_usd=5,
                            maintenance_margin_rate=0.005),
    )
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"strategy_id": strategy_id, "input_sha256": fingerprints,
                "inputs": {k: str(v.resolve()) for k, v in paths.items()},
                "config": config.model_dump(mode="json"), "cost_model": asdict(costs),
                "authority": "offline_simulated_only", "performance_eligible": False,
                "source_policy": preflight["source_policy"], "input_preflight": preflight}
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
    result["source_policy"] = preflight["source_policy"]
    (output / "report.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", required=True, choices=[BTC_STRATEGY_ID, ETH_STRATEGY_ID])
    parser.add_argument("--candles", type=Path, required=True)
    parser.add_argument("--h4", type=Path, required=True)
    parser.add_argument("--daily", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-policy", choices=SOURCE_POLICIES, default="canonical_only")
    parser.add_argument("--check-inputs-only", action="store_true")
    args = parser.parse_args()
    if args.check_inputs_only:
        if args.output is not None:
            parser.error("--check-inputs-only writes nothing; omit --output")
        result = preflight_bundle(strategy_id=args.strategy, candles=args.candles,
            h4=args.h4, daily=args.daily, source_policy=args.source_policy)
        print(json.dumps(result, indent=2, default=str))
        if result["issues"]:
            raise SystemExit(2)
        return
    if args.output is None:
        parser.error("--output is required for replay")
    result = asyncio.run(replay_bundle(strategy_id=args.strategy, candles=args.candles,
                                     h4=args.h4, daily=args.daily, output=args.output,
                                     source_policy=args.source_policy))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
