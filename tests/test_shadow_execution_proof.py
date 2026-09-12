"""Isolated plumbing proof, NOT OOS/venue evidence or a registered strategy.

All candles, quotes and edge estimates below are synthetic test inputs. They
exercise canonical validation, not historical tape provenance. Every artifact
lives under pytest's tmp_path; never point this fixture at operational logs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from vnedge.dashboard.trade_journal import build_trade_journal
from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.data.schemas import normalize_candles
from vnedge.exchange.venue_specs import venue_fill_model
from vnedge.execution.evidence import CostDecisionEvidence, DecisionEnvelope, ExecutionEvidence
from vnedge.execution.fill_ledger import FillLedger, verify_chain
from vnedge.execution.journal import DecisionJournal, verify_journal_chain
from vnedge.execution.order_manager import OrderManager
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.position_sizer import SymbolLimits
from vnedge.risk.risk_manager import MarketState, PreTradeRiskGateway
from vnedge.runtime.execution_contract import DataClock, ExecutionContext, ExecutionStage
from vnedge.runtime.execution_kernel import build_kernel
from vnedge.runtime.live_paper import LivePaperSession
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.scanner_contracts import scanner_runtime_contract

SYMBOL = "BTC/USD:USD"
LANE = "fixture_shadow_execution_proof"
OPEN = datetime(2026, 9, 10, 12, tzinfo=UTC)
NOW = OPEN + timedelta(minutes=15, milliseconds=100)


class _ClockMeta(type):
    def __instancecheck__(cls, value: object) -> bool:
        return isinstance(value, datetime)


class FixtureClock(datetime, metaclass=_ClockMeta):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


class FixtureStrategy(BaseStrategy):
    strategy_id = "fixture_shadow_execution_proof_v1"
    warmup_bars = 2

    def __init__(self, edge: float | None = 100.0, *, omit_hash: bool = False) -> None:
        self.edge = edge
        self.omit_hash = omit_hash

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        result = candles.copy()
        if self.omit_hash:
            result = result.drop(columns=["content_sha256"], errors="ignore")
        return result

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent:
        return SignalIntent(
            "long",
            stop_price=95.0,
            take_profit_price=110.0,
            reason="SYNTHETIC PLUMBING FIXTURE - NOT A MARKET CLAIM",
            expected_gross_edge_bps=self.edge,
            edge_model_id="fixture_only_not_oos" if self.edge is not None else None,
        )


class FixtureFeed:
    exchange_id = "delta_india"
    quote = (99.99, 100.01)
    funding_rate = 0.0

    def __init__(self, row: list[float | int]) -> None:
        self.closed_candles: asyncio.Queue[list[float | int]] = asyncio.Queue()
        self.closed_candles.put_nowait(row)

    def staleness_seconds(self, now: datetime | None = None) -> float:
        return 0.1

    def market_state(self) -> MarketState:
        return MarketState(
            symbol=SYMBOL,
            last_update=NOW,
            spread_bps=2.0,
            estimated_slippage_bps=2.0,
            funding_rate=0.0,
            exchange_healthy=True,
        )


@dataclass
class Proof:
    session: LivePaperSession
    exchange: SimulatedExchange
    journal: DecisionJournal
    ledger: FillLedger
    root: Path

    def rows(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.journal.read_all() if r["kind"] == kind]

    def projection(self) -> dict[str, Any]:
        return build_trade_journal(snapshot={}, journal_dir=self.root, lane=LANE)


def build_proof(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    edge: float | None = 100.0,
    omit_hash: bool = False,
    impossible_size: bool = False,
) -> Proof:
    # Freeze receipt time, but never replace production signal binding, cost,
    # sizing, risk, submit or projection methods with approving mocks.
    monkeypatch.setattr("vnedge.runtime.live_paper.datetime", FixtureClock)

    def deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("shadow proof must never connect to a network")

    monkeypatch.setattr("socket.socket.connect", deny_network)
    raw = [int(OPEN.timestamp() * 1000), 100.0, 101.0, 99.0, 100.0, 5.0]
    history = normalize_candles(
        [
            [
                int((OPEN - timedelta(minutes=15 * i)).timestamp() * 1000),
                100.0,
                101.0,
                99.0,
                100.0,
                5.0,
            ]
            for i in range(5, 0, -1)
        ]
    )
    candle = Candle(
        symbol=SYMBOL,
        timeframe="15m",
        open_time=OPEN,
        close_time=OPEN + timedelta(minutes=15),
        open=Decimal(100),
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal(100),
        volume=Decimal(5),
        quote_volume=Decimal(500),
        trade_count=5,
    )
    store = CandleParquetStore(root / "fixture_candles", exchange="delta_india")
    store.upsert([candle])
    config = RunnerConfig(
        mode=RunnerMode.SHADOW,
        symbol=SYMBOL,
        timeframe="15m",
        execution_cost_exchange_id="delta_india",
        # Unregistered 15m fixture uses the existing Delta scalp cost family.
        # This is not the hot HTF contract and cannot validate its economics.
        limits=(
            SymbolLimits(
                min_qty=100.0, qty_step=100.0, min_notional_usd=5.0, maintenance_margin_rate=0.005
            )
            if impossible_size
            else SymbolLimits(
                min_qty=0.001,
                qty_step=0.001,
                min_notional_usd=5.0,
                maintenance_margin_rate=0.005,
            )
        ),
    )
    exchange = SimulatedExchange(venue_fill_model("delta_india"), config.starting_equity_usd)
    journal = DecisionJournal(root / f"{LANE}.journal.jsonl")
    ledger = FillLedger(root / f"{LANE}.fills.jsonl")
    gateway = PreTradeRiskGateway(config.risk, KillSwitch(kill_file=root / "KILL"))
    manager = OrderManager(gateway, journal, PaperBroker(exchange))
    session = LivePaperSession(
        FixtureStrategy(edge, omit_hash=omit_hash),
        FixtureFeed(raw),
        history,
        config,
        gateway=gateway,
        order_manager=manager,
        exchange=exchange,
        journal=journal,
        fill_ledger=ledger,
        canonical_candle_store=store,
        trial_meta={"trial_id": LANE, "evidence_kind": "synthetic_plumbing_only"},
    )
    # Same simulated authority, explicit replay clock; no real feed is started.
    context = ExecutionContext(DataClock.REPLAY, ExecutionStage.SHADOW)
    session.execution_context = context
    session.execution_kernel.context = context
    assert scanner_runtime_contract(FixtureStrategy.strategy_id) is None
    return Proof(session, exchange, journal, ledger, root)


async def test_shadow_proof_accept_fill_exit_and_journal_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = build_proof(tmp_path, monkeypatch)
    result = await proof.session.run(max_bars=1)
    assert result.signals_generated == result.orders_submitted == 1
    assert len(proof.exchange.get_positions()) == 1
    assert proof.session.last_eval["data_clock"] == "replay"
    assert proof.session.last_eval["data_source"]["candle_source"] == "canonical_tick_lake"
    arm = proof.rows("decision_armed")[0]["payload"]
    entry = proof.rows("order_intent")[0]["payload"]
    evidence = entry["execution_evidence"]
    assert evidence["decision_id"] == arm["decision_id"]
    assert evidence["htf_snapshot_id"] == arm["snapshot_id"]
    assert evidence["cost_decision"]["approved"] is True
    assert evidence["cost_decision"]["profile"] == "delta_scalp"
    assert proof.rows("risk_decision")[0]["payload"]["approved"] is True
    entry_fill = proof.exchange.get_fills()[0]
    assert entry_fill.fee_usd > 0
    assert entry_fill.price > proof.session.feed.quote[1]
    assert entry["intent"]["quantity"] <= (
        proof.session.config.starting_equity_usd
        * proof.session.config.risk.risk_per_trade_pct
        / 100
        / (proof.session.feed.quote[1] - 95.0)
    )
    ordered_kinds = [r["kind"] for r in proof.journal.read_all()]
    assert ordered_kinds.index("decision_armed") < ordered_kinds.index("risk_decision")
    assert ordered_kinds.index("risk_decision") < ordered_kinds.index("order_intent")
    assert ordered_kinds.index("order_intent") < ordered_kinds.index("order_submitted")

    # A restarted manager must recognize the journaled attempt; no second fill
    # or newly minted venue ID when the same decision is presented again.
    original_order = proof.session.om.orders[entry_fill.client_order_id]
    restart = OrderManager(
        proof.session.gateway,
        proof.journal,
        PaperBroker(proof.exchange),
    )
    replay_kernel = build_kernel(
        proof.session.execution_context,
        restart,
        proof.session.execution_kernel.adapter_kind,
    )
    duplicate = await replay_kernel.submit(
        original_order.intent,
        proof.session.tracker.account_state(),
        proof.session.feed.market_state(),
        now=NOW,
        evidence=ExecutionEvidence.from_decision(
            DecisionEnvelope.from_dict(arm),
            cost_decision=CostDecisionEvidence(**evidence["cost_decision"]),
        ),
    )
    assert duplicate.client_order_id is None
    assert len(proof.exchange.get_fills()) == 1
    assert (
        proof.rows("duplicate_intent_dropped")[-1]["payload"]["existing_order"]
        == entry_fill.client_order_id
    )

    # Exit stays possible even with the entry kill switch active.
    (tmp_path / "KILL").touch()
    exit_order = await proof.session._submit_exit("fixture_exit", 1, NOW)
    assert exit_order is not None and exit_order.intent.reduce_only
    assert proof.exchange.get_positions() == []
    proof.session._ledger_new_fills(NOW)
    assert len(proof.exchange.get_fills()) == 2
    assert verify_chain(proof.ledger.path).ok
    assert verify_journal_chain(proof.journal.path).ok
    assert len(proof.rows("order_submitted")) == 2
    view = proof.projection()
    assert len(view["orders"]) == 2
    assert all(o["path_id"] == "kernel_v1" for o in view["orders"])
    assert all(o["decision_id"] for o in view["orders"])
    assert view["closed_trades"]
    trade = view["closed_trades"][0]
    assert trade["decision_id"] == arm["decision_id"]
    assert trade["permission_snapshot_id"] == arm["snapshot_id"]
    assert trade["entry_clock"] == "next_15m_open"

    # Operational fleet projection excludes the fixture lane even if given
    # the fixture directory. No fixture files are written to live log paths.
    operational = build_trade_journal(
        snapshot={"lanes": [{"lane_id": "real_lane"}]},
        journal_dir=tmp_path,
    )
    assert operational["orders"] == []
    assert operational["closed_trades"] == []


@pytest.mark.parametrize(
    "edge,reason",
    [
        (None, "edge_estimate_missing"),
        (1.0, "net "),
    ],
)
async def test_shadow_proof_cost_reject_has_arm_but_no_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edge: float | None,
    reason: str,
) -> None:
    proof = build_proof(tmp_path, monkeypatch, edge=edge)
    result = await proof.session.run(max_bars=1)
    assert result.signals_generated == 1
    assert result.orders_submitted == 0
    assert proof.rows("decision_armed")
    rejection = proof.rows("cost_rejected")[0]["payload"]
    assert reason in rejection["reason"]
    assert not proof.rows("order_intent")
    assert not proof.exchange.get_fills()
    assert proof.projection()["orders"] == []


async def test_shadow_proof_missing_hash_refuses_arm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = build_proof(tmp_path, monkeypatch, omit_hash=True)
    result = await proof.session.run(max_bars=1)
    assert result.signals_generated == result.orders_submitted == 0
    assert proof.rows("entry_evidence_rejected")
    assert not proof.rows("decision_armed")
    assert not proof.exchange.get_fills()


async def test_shadow_proof_sizing_rejection_never_inflates_quantity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = build_proof(tmp_path, monkeypatch, impossible_size=True)
    result = await proof.session.run(max_bars=1)
    assert result.signals_generated == 1
    assert result.orders_submitted == 0
    assert proof.rows("sizing_rejected")
    assert not proof.rows("order_intent")
    assert not proof.exchange.get_fills()


async def test_shadow_proof_risk_reject_does_not_reach_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = build_proof(tmp_path, monkeypatch)
    (tmp_path / "KILL").touch()
    result = await proof.session.run(max_bars=1)
    assert result.signals_generated == 1
    assert result.orders_submitted == 0
    risk = proof.rows("risk_decision")[0]["payload"]
    assert risk["approved"] is False
    assert risk["failed_checks"]
    assert not proof.rows("order_submitted")
    assert not proof.exchange.get_fills()


def test_hot_contracts_report_missing_edge_without_inventing_an_estimate() -> None:
    for symbol in ("BTCUSD", "ETHUSD"):
        contract = scanner_runtime_contract(f"htf_regime_continuation_15m_v2__{symbol}")
        assert contract is not None
        # Current release blocker, not a mandate to keep the fields null.
        # Replace this expectation only alongside a reviewed genuine artifact.
        assert contract.edge_model_id is None
        assert contract.oos_gross_edge_bps is None
