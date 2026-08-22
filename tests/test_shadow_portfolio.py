from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.execution.journal import DecisionJournal
from vnedge.runtime.shadow_portfolio import ShadowPortfolioGate

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)


def _intent(journal: DecisionJournal, key: str, *, symbol: str, side: str) -> None:
    journal.append(
        "shadow_intent",
        {
            "intent_key": key,
            "approved": True,
            "margin_usd": 300,
            "intent": {
                "symbol": symbol,
                "side": side,
                "notional_usd": 3000,
                "leverage": 10,
            },
        },
    )


def test_shared_gate_blocks_conflicts_margin_and_daily_loss(tmp_path):
    a = DecisionJournal(tmp_path / "lane_a.journal.jsonl")
    _intent(a, "a-1", symbol="BTC/USDT:USDT", side="long")
    gate = ShadowPortfolioGate(
        journal_dir=tmp_path,
        lane_ids=("lane_a", "lane_b"),
        equity_usd=Decimal(1000),
        daily_loss_limit_usd=Decimal(20),
    )

    conflict = gate.evaluate_entry(
        lane_id="lane_b",
        symbol="BTC/USDT:USDT",
        side="short",
        margin_usd=Decimal(100),
        now=NOW,
    )
    assert not conflict.allowed
    assert conflict.reason == "opposite_side_conflict"

    exhausted = gate.evaluate_entry(
        lane_id="lane_b",
        symbol="ETH/USDT:USDT",
        side="long",
        margin_usd=Decimal(701),
        now=NOW,
    )
    assert not exhausted.allowed
    assert exhausted.reason == "shared_margin_exhausted"

    a.append(
        "shadow_outcome",
        {
            "intent_key": "a-1",
            "virtual_net_usd": -25,
            "bar_ts": NOW.isoformat(),
        },
    )
    halted = gate.evaluate_entry(
        lane_id="lane_b",
        symbol="ETH/USDT:USDT",
        side="long",
        margin_usd=Decimal(100),
        now=NOW,
    )
    assert not halted.allowed
    assert halted.reason == "shared_daily_loss_halt"
    assert halted.daily_net_usd == Decimal(-25)


def test_shared_gate_reserves_approved_margin_atomically(tmp_path):
    gate = ShadowPortfolioGate(
        journal_dir=tmp_path,
        lane_ids=("lane_a", "lane_b"),
        equity_usd=Decimal(1000),
        daily_loss_limit_usd=Decimal(20),
    )

    first = gate.evaluate_entry(
        lane_id="lane_a",
        symbol="BTC/USDT:USDT",
        side="long",
        margin_usd=Decimal(300),
        now=NOW,
        intent_key="atomic-1",
    )
    assert first.allowed

    conflict = gate.evaluate_entry(
        lane_id="lane_b",
        symbol="BTC/USDT:USDT",
        side="short",
        margin_usd=Decimal(100),
        now=NOW,
        intent_key="atomic-2",
    )
    assert not conflict.allowed
    assert conflict.reason == "opposite_side_conflict"

    over_budget = gate.evaluate_entry(
        lane_id="lane_b",
        symbol="ETH/USDT:USDT",
        side="long",
        margin_usd=Decimal(701),
        now=NOW,
        intent_key="atomic-3",
    )
    assert not over_budget.allowed
    assert over_budget.reason == "shared_margin_exhausted"


def test_orphaned_atomic_reservation_self_recovers(tmp_path):
    gate = ShadowPortfolioGate(
        journal_dir=tmp_path,
        lane_ids=("lane_a", "lane_b"),
        equity_usd=Decimal(1000),
        daily_loss_limit_usd=Decimal(20),
    )
    assert gate.evaluate_entry(
        lane_id="lane_a",
        symbol="BTC/USDT:USDT",
        side="long",
        margin_usd=Decimal(1000),
        now=NOW,
        intent_key="crashed-before-local-wal",
    ).allowed

    recovered = gate.evaluate_entry(
        lane_id="lane_b",
        symbol="ETH/USDT:USDT",
        side="long",
        margin_usd=Decimal(100),
        now=NOW + timedelta(minutes=3),
        intent_key="after-timeout",
    )

    assert recovered.allowed
    assert recovered.active_margin_usd == 0
