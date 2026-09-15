"""Offline preregistered paper-path probe; no production registration or network."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.cost_gate import CostGate, CostProfile
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.paper_runner import PaperRunner
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

HERE = Path(__file__).resolve().parent
SID = "consolidation_scalp_5m_paper_probe_v1__BTCUSD"
COST_HASH = "e04d112e35708157a77fc5b5721e4e2c4b2c0ce6d05c71b65bff3bf443e7b13a"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: object) -> None:
    with path.open("x") as out:
        json.dump(value, out, indent=2, default=str, allow_nan=False)


class Probe(BaseStrategy):
    strategy_id = SID
    warmup_bars = 1

    def __init__(self, event: dict) -> None:
        self.event = event

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return candles.copy()

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if pd.Timestamp(df.iloc[index].timestamp) != pd.Timestamp(self.event["decision_open"]):
            return None
        return SignalIntent(side="long", stop_price=self.event["stop_price"],
                            take_profit_price=self.event["target_price"],
                            reason="frozen baseline episode " + self.event["episode_id"])


async def run(root: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=False)
    generator = HERE.parent / "vwap_consolidation_20260914/replay.py"
    spec = importlib.util.spec_from_file_location("frozen_consolidation", generator)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Resolve the same versioned profile used by the frozen source contract.
    if module.COST_HASH != COST_HASH:
        raise ValueError("cost_contract_changed")
    gate = CostGate(CostProfile.DELTA_SCALP_V2)
    quote = gate.evaluate(signal_edge_bps=0, side="long", urgency="taker",
                          expected_holding_seconds=1800, symbol="BTC/USD:USD")
    if quote.cost.cost_config_sha256 != COST_HASH:
        raise ValueError("cost_profile_changed")
    save(out / "registration.json", {
        "registered_at": datetime.now(UTC).isoformat(), "strategy_id": SID,
        "exchange": "delta_india", "symbol": "BTCUSD", "timeframe": "5m",
        "entry_clock": "next_5m_open", "context_timeframes": [],
        "contract_sha256": sha(HERE / "CONTRACT.md"), "generator_sha256": sha(generator),
        "harness_sha256": sha(Path(__file__)),
        "runner_sha256": sha(Path(__file__).resolve().parents[3] / "src/vnedge/runtime/paper_runner.py"),
        "cost_profile": quote.cost.model_dump(mode="json"), "source_root": str(root),
        "mode": "offline_paper_probe", "performance_eligible": False,
        "can_trade": False, "can_promote": False,
    })
    frame, _, manifest = module.load(root, "BTCUSD")
    save(out / "input_manifest.json", manifest)
    events, counts = module.generate(frame, "BTCUSD")
    reports, rejected, censored = [], Counter(), []
    for number, event in enumerate(events):
        ts = pd.Timestamp(event["decision_open"])
        times = pd.date_range(ts - pd.Timedelta(minutes=5), periods=9, freq="5min")
        window = frame.reindex(times)
        if not window.eligible.eq(True).all():
            censored.append({"episode_id": event["episode_id"], "reason": "execution_window_incomplete"})
            continue
        window = window.rename_axis("timestamp").reset_index()
        cfg = RunnerConfig(mode=RunnerMode.PAPER, symbol="BTC/USD:USD", timeframe="5m",
                           max_holding_bars=6, allow_partial_tp=False, reconcile_every_bars=1,
                           spread_bps=1, slippage_est_bps=2.5)
        # Assumed paper execution, never represented as observed BBO/venue fills.
        exchange = SimulatedExchange(FillModel(taker_fee_bps=5.9, slippage_bps=2.5), cfg.starting_equity_usd)
        journal = DecisionJournal(out / f"episode_{number:04d}.journal.jsonl")
        gateway = PreTradeRiskGateway(cfg.risk, KillSwitch(kill_file=out / "KILL"))
        manager = OrderManager(gateway, journal, PaperBroker(exchange))
        runner = PaperRunner(Probe(event), window, None, cfg, gateway=gateway,
                             order_manager=manager, exchange=exchange, journal=journal,
                             entry_cost_gate=gate, require_canonical_truth=True)
        result = await runner.run()
        records = journal.read_all()
        rejected.update(r["payload"].get("reason", "unknown") for r in records if r["kind"] == "cost_rejected")
        reports.append({"episode_id": event["episode_id"], "report": result.to_dict(),
                        "journal_sha256": sha(journal.path)})
    save(out / "results.json", {
        "strategy_id": SID, "generated_at": datetime.now(UTC).isoformat(),
        "generator_counts": counts, "episodes": len(events), "attempted": len(reports),
        "censored": censored, "cost_rejections": dict(rejected), "reports": reports,
        "signals": sum(r["report"]["signals_generated"] for r in reports),
        "orders": sum(r["report"]["orders_submitted"] for r in reports),
        "fills": sum(r["report"]["fills"] for r in reports),
        "settled_net_usd": None, "funding_status": "unavailable",
        "verdict": "RESEARCH_ONLY_NO_BOUND_EDGE_ESTIMATE", "edge_found": False,
        "can_trade": False, "can_promote": False, "performance_eligible": False,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.root, args.out))
