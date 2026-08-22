from datetime import UTC, datetime
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
