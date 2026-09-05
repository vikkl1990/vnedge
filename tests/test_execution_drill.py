"""Mainnet execution drill — gates, caps, lifecycle, flat-precondition."""

from datetime import UTC, datetime

import pytest

from vnedge.config.settings import Settings
from vnedge.execution.evidence import DecisionEnvelope
from vnedge.execution.journal import DecisionJournal
from vnedge.runtime.execution_drill import (
    _HARD_MAX_DRILL_NOTIONAL,
    DrillConfig,
    run_execution_drill,
)
from vnedge.strategy.arm_evidence import freeze_permission_from_row

LIVE_ENV = {
    "trading_mode": "live_small",
    "live_trading_enabled": True,
    "confirm_live_trading": "I_UNDERSTAND_THIS_IS_HIGH_RISK",
}


class FakeAdapter:
    """Happy-path venue: accepts, shows open, cancels clean, stays flat."""

    def __init__(self, *, positions=(), open_orders=(), mid=0.08, equity=250.0):
        self._positions = list(positions)
        self._open_orders = list(open_orders)
        self._mid = mid
        self._equity = equity
        self.submitted = []
        self.cancelled = []
        self.closed = False

    async def fetch_balance(self):
        # A realistic small LIVE account: the drill now routes through the real
        # gateway, whose min_equity gate ($100) correctly rejects sub-$100.
        return {"total_usd": self._equity, "USDT": self._equity}

    async def fetch_positions(self, symbol):
        return self._positions

    async def fetch_open_orders(self, symbol):
        return self._open_orders

    async def fetch_mid_price(self, symbol):
        return self._mid

    def amount_to_precision(self, symbol, amount):
        return float(int(amount))  # whole-unit steps, rounded DOWN

    async def submit_order(self, order):
        self.submitted.append(order)
        return "EX123"

    async def fetch_order_status(self, order):
        return {"status": "open", "filled": 0.0}

    async def cancel_order(self, order):
        self.cancelled.append(order)
        return "canceled"

    async def close(self):
        self.closed = True


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VNEDGE_EXEC_API_KEY", "k")
    monkeypatch.setenv("VNEDGE_EXEC_API_SECRET", "s")
    monkeypatch.setenv("PRE_LIVE_LADDER_ATTESTED", "1")
    monkeypatch.setenv("KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.setenv("DECISION_JOURNAL", str(tmp_path / "dj.jsonl"))
    monkeypatch.chdir(tmp_path)


def _decision(symbol: str = "DOGE/USDT:USDT") -> DecisionEnvelope:
    snapshot = freeze_permission_from_row(
        {
            "timestamp": datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
            "open": 0.08,
            "high": 0.081,
            "low": 0.079,
            "close": 0.08,
            "volume": 1000.0,
            "quote_volume": 80.0,
            "trade_count": 100,
            "is_closed": True,
            "data_quality": "ok",
            "candle_source": "canonical_tick_lake",
        },
        decision_timeframe="1m",
        context_timeframes=(),
        allow_long=True,
        allow_short=False,
        reason="bounded_live_drill",
    )
    return DecisionEnvelope.create(
        strategy_id="execution_drill_v1",
        symbol=symbol,
        timeframe="1m",
        side="long",
        permission_snapshot=snapshot,
        entry_clock="manual_drill",
    )


async def test_drill_refuses_without_three_gates(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings()  # backtest mode — gates closed
    report = await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=FakeAdapter,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
    )
    assert not report.cleared
    assert report.steps[0].name == "live_gates" and not report.steps[0].ok


async def test_live_drill_refuses_to_fabricate_a_decision_envelope(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter()

    report = await run_execution_drill(
        settings,
        DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=lambda: fake,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
    )

    assert not report.cleared
    assert any(
        step.name == "decision_envelope" and not step.ok for step in report.steps
    )
    assert fake.submitted == []


async def test_drill_happy_path_clears(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter()
    report = await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm", order_notional_usd=8.0),
        adapter_factory=lambda: fake,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
        decision_envelope=_decision(),
    )
    assert report.cleared, [s for s in report.steps if not s.ok]
    assert len(fake.submitted) == 1
    order = fake.submitted[0]
    assert order.intent.order_type == "limit"
    assert order.intent.limit_price == pytest.approx(0.08 * 0.85)  # 15% below mid
    assert order.intent.notional_usd <= _HARD_MAX_DRILL_NOTIONAL
    assert order.intent.leverage == 1.0
    assert fake.cancelled == fake.submitted
    assert fake.closed


async def test_drill_notional_hard_cap(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter(mid=0.08)
    report = await run_execution_drill(
        settings,
        DrillConfig(exchange_id="binanceusdm", order_notional_usd=10_000.0),
        adapter_factory=lambda: fake,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
        decision_envelope=_decision(),
    )
    assert report.cleared
    assert fake.submitted[0].intent.notional_usd <= _HARD_MAX_DRILL_NOTIONAL


async def test_drill_refuses_on_existing_exposure(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter(positions=[{"contracts": 1.0}])
    report = await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=lambda: fake,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
        decision_envelope=_decision(),
    )
    assert not report.cleared
    assert fake.submitted == []  # never places an order near real exposure


async def test_drill_blocked_by_checklist(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    monkeypatch.delenv("VNEDGE_EXEC_API_KEY")  # checklist: credentials missing
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter()
    report = await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=lambda: fake,
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
    )
    assert not report.cleared
    assert any(s.name == "pre_live_checklist" and not s.ok for s in report.steps)
    assert fake.submitted == []


async def test_delta_drill_requires_native_read_surfaces(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    report = await run_execution_drill(
        settings,
        DrillConfig(exchange_id="delta_india", symbol="BTC/USD:USD"),
        journal=DecisionJournal(tmp_path / "drill.jsonl"),
    )
    assert not report.cleared
    check = next(s for s in report.steps if s.name == "delta_native_drill")
    assert not check.ok
    assert "balance" in check.detail and "position" in check.detail


async def test_drill_order_routes_through_the_gateway(tmp_path, monkeypatch):
    # A sub-min-equity account must be REJECTED by the gateway inside the drill —
    # proof the order is evaluated, not bypassed (the invariant Gap 1 restores).
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter(equity=42.0)  # below the $100 min_equity gate
    report = await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=lambda: fake, journal=DecisionJournal(tmp_path / "d.jsonl"),
        decision_envelope=_decision(),
    )
    assert not report.cleared
    ga = [s for s in report.steps if s.name == "gateway_approved"]
    assert ga and ga[0].ok is False
    assert fake.submitted == []  # rejected before ever reaching the venue


async def test_drill_client_order_id_is_uuid_not_timestamp(tmp_path, monkeypatch):
    # OrderManager mints a uuid client id; the old drill derived it from
    # time.time() (an idempotency-rule violation). Verify it's not numeric.
    _env(monkeypatch, tmp_path)
    settings = Settings(**LIVE_ENV)
    fake = FakeAdapter(equity=250.0)
    await run_execution_drill(
        settings, DrillConfig(exchange_id="binanceusdm"),
        adapter_factory=lambda: fake, journal=DecisionJournal(tmp_path / "d.jsonl"),
        decision_envelope=_decision(),
    )
    assert len(fake.submitted) == 1
    coid = fake.submitted[0].client_order_id
    assert not coid.replace("drill", "").isdigit()  # not a bare timestamp
    assert "-" in coid or len(coid) >= 16  # uuid-shaped
