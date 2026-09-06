"""Order manager pipeline — journaling order, duplicates, TIMEOUT_UNKNOWN policy."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.execution.evidence import ExecutionEvidence
from vnedge.execution.idempotency import (
    IntentRegistry,
    make_intent_key,
    mint_client_order_id,
)
from vnedge.execution.journal import DecisionJournal, verify_journal_chain
from vnedge.execution.order_manager import (
    AdapterRejection,
    AdapterTimeout,
    OrderManager,
)
from vnedge.execution.order_state import OrderState as S
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import (
    AccountState,
    MarketState,
    OrderIntent,
    PreTradeRiskGateway,
)

# --- Fakes / fixtures -----------------------------------------------------------

class AckAdapter:
    """Acks everything; records the journal kinds visible at submit time so
    tests can prove intent-journaling happens BEFORE submission."""

    def __init__(self, journal: DecisionJournal):
        self.journal = journal
        self.submissions: list[tuple[str, list[str]]] = []

    async def submit_order(self, order):
        kinds = [r["kind"] for r in self.journal.read_all()]
        self.submissions.append((order.client_order_id, kinds))
        return f"ex_{len(self.submissions)}"


class TimeoutAdapter:
    async def submit_order(self, order):
        raise AdapterTimeout("no ack within deadline")


class RejectAdapter:
    async def submit_order(self, order):
        raise AdapterRejection("insufficient margin at venue")


@pytest.fixture
def journal(tmp_path):
    return DecisionJournal(tmp_path / "journal.jsonl")


@pytest.fixture
def gateway(tmp_path):
    return PreTradeRiskGateway(RiskConfig(), KillSwitch(kill_file=tmp_path / "KILL"))


def intent(**overrides) -> OrderIntent:
    defaults = {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "quantity": 0.001,
        "notional_usd": 110.0,
        "leverage": 3.0,
        "reduce_only": False,
        "strategy_id": "test",
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def account(**overrides) -> AccountState:
    defaults = {
        "equity_usd": 800.0,
        "daily_pnl_usd": -5.0,
        "peak_equity_usd": 850.0,
        "open_positions": 0,
        "exposure_by_symbol_usd": {},
        "total_exposure_usd": 0.0,
    }
    defaults.update(overrides)
    return AccountState(**defaults)


def market() -> MarketState:
    return MarketState(
        symbol="BTC/USDT:USDT",
        last_update=datetime.now(UTC) - timedelta(seconds=1),
        spread_bps=1.0, estimated_slippage_bps=2.0,
        funding_rate=0.0001, exchange_healthy=True,
    )


def key(i: int = 0, side: str = "long") -> str:
    return make_intent_key("test", "BTC/USDT:USDT", side,
                           pd.Timestamp(1_750_000_000_000 + i * 3_600_000, unit="ms", tz="UTC"))


def evidence(*, side: str = "long", snapshot_id: str | None = None) -> ExecutionEvidence:
    return ExecutionEvidence.create(
        strategy_id="test_v1",
        symbol="BTC/USDT:USDT",
        timeframe="15m",
        bar_open=datetime(2026, 8, 28, tzinfo=UTC),
        side=side,
        htf_snapshot_id=snapshot_id,
        candle_source="parquet",
        entry_clock="next_15m_open",
    )


# --- Idempotency primitives -------------------------------------------------------

def test_intent_key_is_deterministic():
    assert key(1) == key(1)
    assert key(1) != key(2)


def test_client_order_ids_are_unique():
    ids = {mint_client_order_id() for _ in range(200)}
    assert len(ids) == 200


def test_order_intent_serialization_excludes_execution_provenance():
    payload = asdict(
        intent(
            strategy_id="legacy_v1",
            permission_snapshot_id="a" * 24,
            permission_snapshot=object(),
        )
    )
    assert "strategy_id" not in payload
    assert "permission_snapshot_id" not in payload
    assert "permission_snapshot" not in payload


def test_decision_id_is_deterministic_and_snapshot_bound():
    first = evidence(snapshot_id="a" * 24)
    assert first.decision_id == evidence(snapshot_id="a" * 24).decision_id
    assert first.decision_id != evidence(snapshot_id="b" * 24).decision_id


def test_registry_blocks_duplicates():
    reg = IntentRegistry()
    assert reg.register("k1", "oid1")
    assert not reg.register("k1", "oid2")
    assert reg.existing_order_id("k1") == "oid1"


# --- Pipeline ----------------------------------------------------------------------

async def test_happy_path_acknowledged(journal, gateway):
    adapter = AckAdapter(journal)
    om = OrderManager(gateway, journal, adapter)
    order = await om.submit(intent(), account(), market(), key(0))
    assert order.state is S.ACKNOWLEDGED
    assert order.exchange_order_id == "ex_1"
    kinds = [r["kind"] for r in journal.read_all()]
    assert kinds == [
        "risk_decision",
        "order_intent",
        "order_submitted",
        "order_acknowledged",
    ]


async def test_intent_journaled_before_venue_submission(journal, gateway):
    adapter = AckAdapter(journal)
    om = OrderManager(gateway, journal, adapter)
    await om.submit(intent(), account(), market(), key(0))
    _, kinds_at_submit = adapter.submissions[0]
    assert "order_intent" in kinds_at_submit  # journaled BEFORE the venue call
    assert "order_submitted" in kinds_at_submit


async def test_risk_rejection_never_reaches_adapter(journal, gateway):
    adapter = AckAdapter(journal)
    om = OrderManager(gateway, journal, adapter)
    bad = intent(leverage=25.0)  # over the 5x cap
    order = await om.submit(bad, account(), market(), key(0))
    assert order.state is S.RISK_REJECTED
    assert order.client_order_id is None
    assert adapter.submissions == []


async def test_kernel_evidence_mints_venue_id_only_after_risk_pass(journal, gateway):
    adapter = AckAdapter(journal)
    om = OrderManager(gateway, journal, adapter)
    ev = evidence()

    approved = await om.submit(intent(), account(), market(), evidence=ev)

    assert approved.intent_key == ev.decision_id
    assert approved.client_order_id is not None
    assert adapter.submissions[0][0] == approved.client_order_id
    rows = journal.read_all()
    risk = next(r["payload"] for r in rows if r["kind"] == "risk_decision")
    submitted = next(r["payload"] for r in rows if r["kind"] == "order_intent")
    assert "client_order_id" not in risk
    assert submitted["client_order_id"] == approved.client_order_id
    assert submitted["execution_evidence"]["decision_id"] == ev.decision_id
    assert submitted["intent"].get("strategy_id") is None


async def test_reconciled_terminal_event_preserves_execution_envelope(journal, gateway):
    om = OrderManager(gateway, journal, TimeoutAdapter())
    ev = evidence()
    order = await om.submit(intent(), account(), market(), evidence=ev)
    assert order.client_order_id is not None

    om.begin_reconciliation(order.client_order_id)
    om.resolve_order(order.client_order_id, S.REJECTED, "venue found no order")

    resolved = [
        row["payload"] for row in journal.read_all() if row["kind"] == "order_resolved"
    ][-1]
    assert resolved["path_id"] == "kernel_v1"
    assert resolved["decision_id"] == ev.decision_id
    assert resolved["execution_evidence"]["execution_contract_id"] == (
        "kernel_v1|parquet|next_15m_open"
    )


async def test_duplicate_intent_dropped(journal, gateway):
    adapter = AckAdapter(journal)
    om = OrderManager(gateway, journal, adapter)
    first = await om.submit(intent(), account(), market(), key(0))
    second = await om.submit(intent(), account(), market(), key(0))
    assert first.state is S.ACKNOWLEDGED
    assert second.state is S.RISK_REJECTED
    assert "duplicate" in second.history[-1].note
    assert len(adapter.submissions) == 1


async def test_venue_rejection_is_terminal(journal, gateway):
    om = OrderManager(gateway, journal, RejectAdapter())
    order = await om.submit(intent(), account(), market(), key(0))
    assert order.state is S.REJECTED
    assert "insufficient margin" in order.history[-1].note


async def test_timeout_unknown_blocks_new_risk_but_not_exits(journal, gateway):
    om = OrderManager(gateway, journal, TimeoutAdapter())
    stuck = await om.submit(intent(), account(), market(), key(0))
    assert stuck.state is S.TIMEOUT_UNKNOWN
    assert om.has_unresolved_orders

    # new risk-increasing order: refused before the gateway even runs
    om._adapter = AckAdapter(journal)
    blocked = await om.submit(intent(), account(), market(), key(1))
    assert blocked.state is S.RISK_REJECTED
    assert "TIMEOUT_UNKNOWN" in blocked.history[-1].note

    # reduce-only exit: flows through
    exit_order = await om.submit(
        intent(reduce_only=True, side="short"),
        account(open_positions=1), market(), key(2, side="short"),
    )
    assert exit_order.state is S.ACKNOWLEDGED


async def test_reconciliation_unblocks_entries(journal, gateway):
    om = OrderManager(gateway, journal, TimeoutAdapter())
    stuck = await om.submit(intent(), account(), market(), key(0))
    om.begin_reconciliation(stuck.client_order_id)
    om.resolve_order(stuck.client_order_id, S.FILLED, "exchange shows filled")
    assert stuck.state is S.FILLED
    assert not om.has_unresolved_orders

    om._adapter = AckAdapter(journal)
    order = await om.submit(intent(), account(), market(), key(1))
    assert order.state is S.ACKNOWLEDGED


async def test_registry_seeded_from_journal_survives_restart(journal, gateway):
    # First process submits an intent -> order_intent journaled durably.
    om1 = OrderManager(gateway, journal, AckAdapter(journal))
    first = await om1.submit(intent(), account(), market(), key(0))
    assert first.state is S.ACKNOWLEDGED

    # A fresh OrderManager (simulating a RESTART) rebuilds its in-memory registry
    # from the journal, so the SAME intent re-presented is caught as a duplicate —
    # no double-book with a fresh client_order_id the venue can't dedupe.
    adapter2 = AckAdapter(journal)
    om2 = OrderManager(gateway, journal, adapter2)
    assert om2._registry.existing_order_id(key(0)) == first.client_order_id  # seeded
    dup = await om2.submit(intent(), account(), market(), key(0))
    assert dup.state is S.RISK_REJECTED
    assert "duplicate" in dup.history[-1].note
    assert adapter2.submissions == []  # never reached the venue


async def test_rejected_reduce_only_resubmission_survives_restart(journal, gateway):
    exit_intent = intent(reduce_only=True, side="short")
    ev = evidence(side="short")
    first_manager = OrderManager(gateway, journal, RejectAdapter())
    first = await first_manager.submit(
        exit_intent,
        account(open_positions=1),
        market(),
        evidence=ev,
    )
    assert first.state is S.REJECTED
    assert first.client_order_id is not None

    adapter = AckAdapter(journal)
    restarted = OrderManager(gateway, journal, adapter)
    second = await restarted.submit(
        exit_intent,
        account(open_positions=1),
        market(),
        evidence=ev,
    )

    assert second.state is S.ACKNOWLEDGED
    assert second.intent_key == first.intent_key
    assert second.client_order_id != first.client_order_id
    intent_rows = [
        row["payload"] for row in journal.read_all() if row["kind"] == "order_intent"
    ]
    assert intent_rows[-1]["retry_of"] == first.client_order_id


async def test_h3_rehydrates_unresolved_order_across_restart(journal, gateway):
    # First process: an entry times out (parked TIMEOUT_UNKNOWN) and is journaled.
    om1 = OrderManager(gateway, journal, TimeoutAdapter())
    stuck = await om1.submit(intent(), account(), market(), key(0))
    assert stuck.state is S.TIMEOUT_UNKNOWN and om1.has_unresolved_orders

    # Restart: a fresh OM on the SAME journal rehydrates the unresolved order so new
    # risk stays blocked until reconciliation — it is not silently forgotten.
    om2 = OrderManager(gateway, journal, AckAdapter(journal))
    assert om2.has_unresolved_orders
    assert om2.orders[stuck.client_order_id].state is S.TIMEOUT_UNKNOWN
    blocked = await om2.submit(intent(), account(), market(), key(1))
    assert blocked.state is S.RISK_REJECTED
    assert "TIMEOUT_UNKNOWN" in blocked.history[-1].note


async def test_h3_resolved_order_not_rehydrated(journal, gateway):
    # A cleanly ACKNOWLEDGED order (paper-style, acks synchronously) must NOT be
    # rehydrated as unresolved — otherwise a restart would wedge the paper fleet.
    om1 = OrderManager(gateway, journal, AckAdapter(journal))
    ackd = await om1.submit(intent(), account(), market(), key(0))
    assert ackd.state is S.ACKNOWLEDGED
    om2 = OrderManager(gateway, journal, AckAdapter(journal))
    assert not om2.has_unresolved_orders   # last kind = order_acknowledged -> skipped


class PlainAckAdapter:
    async def submit_order(self, order):
        return "ex_plain"


async def test_unavailable_journal_means_exits_only(tmp_path, gateway):
    dead_journal = DecisionJournal(tmp_path)  # path IS a directory -> unwritable
    assert not dead_journal.available
    om = OrderManager(gateway, dead_journal, PlainAckAdapter())

    entry = await om.submit(intent(), account(), market(), key(0))
    assert entry.state is S.RISK_REJECTED
    assert "journal unavailable" in entry.history[-1].note

    exit_order = await om.submit(
        intent(reduce_only=True, side="short"),
        account(open_positions=1), market(), key(1, side="short"),
    )
    assert exit_order.state is S.ACKNOWLEDGED  # getting out is never blocked


def test_journal_roundtrip(tmp_path):
    j = DecisionJournal(tmp_path / "j.jsonl")
    assert j.append("test_event", {"a": 1})
    assert j.append("test_event", {"a": 2})
    records = j.read_all()
    assert len(records) == 2
    assert records[1]["payload"]["a"] == 2
    assert records[0]["kind"] == "test_event"
    assert [row["seq"] for row in records] == [0, 1]
    assert all(row["schema_version"] == 2 for row in records)
    report = verify_journal_chain(j.path)
    assert report.ok
    assert report.chained_records == 2


def test_journal_chain_detects_tampered_record(tmp_path):
    path = tmp_path / "j.jsonl"
    journal = DecisionJournal(path)
    assert journal.append("test_event", {"a": 1})
    row = json.loads(path.read_text().strip())
    row["payload"]["a"] = 2
    path.write_text(json.dumps(row) + "\n")

    report = verify_journal_chain(path)

    assert not report.ok
    assert report.first_bad_line == 1
    assert report.reason == "journal record hash mismatch"


def test_tampered_final_wal_record_is_quarantined_at_startup(tmp_path):
    path = tmp_path / "j.jsonl"
    journal = DecisionJournal(path)
    assert journal.append("test_event", {"a": 1})
    row = json.loads(path.read_text().strip())
    row["payload"]["a"] = 2
    path.write_text(json.dumps(row) + "\n")

    recovered = DecisionJournal(path)

    assert recovered.recovery_degraded
    assert recovered.quarantine_path is not None
    assert recovered.read_all() == []


def test_journal_chain_accepts_legacy_prefix_then_v2(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text('{"ts":"x","kind":"legacy","payload":{}}\n')
    journal = DecisionJournal(path)
    assert journal.append("current", {"a": 1})

    report = verify_journal_chain(path)

    assert report.ok
    assert report.legacy_records == 1
    assert report.chained_records == 1


def test_kernel_journal_stamps_one_path_and_refuses_conflicts(tmp_path):
    j = DecisionJournal(tmp_path / "kernel.jsonl", path_id="kernel_v1")
    assert j.append("test_event", {"a": 1})
    assert "path_id" not in j.read_all()[0]["payload"]
    assert j.append("order_probe", {"a": 2})
    assert "path_id" not in j.read_all()[1]["payload"]
    assert j.append("order_probe", {"a": 3, "path_id": "kernel_v1"})
    assert j.read_all()[2]["payload"]["path_id"] == "kernel_v1"

    assert not j.append("test_event", {"path_id": "legacy_shadow"})
    assert not j.available


def test_complete_wal_tail_without_newline_is_repaired(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text('{"ts":"x","kind":"test_event","payload":{"a":1}}')

    recovered = DecisionJournal(path)

    assert recovered.available
    assert not recovered.recovery_degraded
    assert path.read_bytes().endswith(b"\n")
    assert recovered.append("test_event", {"a": 2})
    assert [row["payload"]["a"] for row in recovered.read_all()] == [1, 2]


async def test_corrupt_wal_tail_is_quarantined_and_entries_fail_closed(tmp_path, gateway):
    path = tmp_path / "j.jsonl"
    initial = DecisionJournal(path)
    assert initial.append("test_event", {"valid": True})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts":"truncated"')

    recovered = DecisionJournal(path)
    assert recovered.recovery_degraded
    assert recovered.quarantine_path is not None
    assert recovered.quarantine_path.exists()
    assert recovered.read_all()[0]["payload"] == {"valid": True}

    om = OrderManager(gateway, recovered, PlainAckAdapter())
    assert om.has_unresolved_orders
    entry = await om.submit(intent(), account(), market(), key(0))
    assert entry.state is S.RISK_REJECTED
    assert "recovery degraded" in entry.history[-1].note

    exit_order = await om.submit(
        intent(reduce_only=True, side="short"),
        account(open_positions=1), market(), key(1, side="short"),
    )
    assert exit_order.state is S.ACKNOWLEDGED

    assert recovered.acknowledge_recovery("venue reconciliation found no open order")
    assert not recovered.recovery_degraded
