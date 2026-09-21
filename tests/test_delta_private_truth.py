"""Private fill evidence is durable, idempotent and never live permission."""

import pytest

from vnedge.exchange.delta_private_truth import (
    DeltaPrivateFillEvidenceStore,
    DeltaPrivateRestBaselineReader,
    DeltaPrivateTruthObserver,
    DeltaRestBaseline,
)
from vnedge.exchange.delta_private_ws import (
    DeltaPrivateContinuityError,
    DeltaPrivateWsClient,
)
from vnedge.execution.fill_ledger import verify_chain


def _client(store):
    client = DeltaPrivateWsClient(
        ("BTC/USD:USD",), api_key="k", api_secret="s",
        on_event=store.record_event,
    )
    client.authenticated = True
    return client


def _fast(trade_id, seq, price="100"):
    return {
        "type": "v2/user_trades", "sy": "BTCUSD", "f": trade_id,
        "se": seq, "p": price, "s": 1, "S": "buy",
    }


def _rest(trade_id, price="100", commission="0.10"):
    return {
        "id": trade_id, "product_symbol": "BTCUSD", "price": price,
        "size": "1", "commission": commission,
    }


def _baseline(fills, *, complete=True, positions=True):
    return DeltaRestBaseline(
        order_snapshots={"BTCUSD": []},
        position_snapshots={"BTCUSD": []} if positions else {},
        fills=fills,
        fills_pagination_exhausted=complete,
    )


def test_gap_frame_fill_persists_before_latch_and_survives_restart(tmp_path):
    path = tmp_path / "private-fills.jsonl"
    store = DeltaPrivateFillEvidenceStore(path, symbols=("BTCUSD",))
    client = _client(store)
    client.ingest(_fast("fill-1", 4))
    with pytest.raises(DeltaPrivateContinuityError, match="4 -> 6"):
        client.ingest(_fast("fill-gap", 6, "101"))
    assert store.trade_ids == {"fill-1", "fill-gap"}
    assert not client.continuity_ok and not store.healthy
    assert verify_chain(path).ok and verify_chain(path).records == 2
    resumed = DeltaPrivateFillEvidenceStore(path, symbols=("BTCUSD",))
    assert resumed.trade_ids == store.trade_ids
    assert resumed.reconcile_restart(_baseline([
        _rest("fill-1"), _rest("fill-missed", "100.5"),
        _rest("fill-gap", "101"),
    ]))
    assert resumed.trade_ids == {"fill-1", "fill-missed", "fill-gap"}
    assert resumed.fees_complete
    records = verify_chain(path).records
    assert resumed.reconcile_restart(_baseline([
        _rest("fill-1"), _rest("fill-missed", "100.5"),
        _rest("fill-gap", "101"),
    ]))
    assert verify_chain(path).records == records


def test_restart_does_not_recover_from_sequence_alone_or_incomplete_rest(tmp_path):
    store = DeltaPrivateFillEvidenceStore(
        tmp_path / "fills.jsonl", symbols=("BTCUSD",),
    )
    _client(store).ingest(_fast("fill-1", 1))
    for baseline, expected in (
        (_baseline([_rest("fill-1")], complete=False), "pagination incomplete"),
        (_baseline([_rest("fill-1")], positions=False), "snapshot missing"),
        (_baseline([]), "missing observed"),
        (_baseline([_rest("fill-1", commission=None)]), "commission absent"),
        (_baseline([_rest("fill-1", price="101")]), "identity mismatch"),
    ):
        with pytest.raises(ValueError, match=expected):
            store.reconcile_restart(baseline)
        assert not store.healthy


def test_duplicate_trade_id_never_double_counts_and_conflict_rejects(tmp_path):
    store = DeltaPrivateFillEvidenceStore(
        tmp_path / "fills.jsonl", symbols=("BTCUSD",),
    )
    client = _client(store)
    client.ingest(_fast("fill-1", 1))
    client.ingest(_fast("fill-1", 2))
    assert verify_chain(store._ledger.path).records == 1
    with pytest.raises(DeltaPrivateContinuityError, match="persistence failed"):
        client.ingest(_fast("fill-1", 3, "101"))
    assert not client.continuity_ok
    assert verify_chain(store._ledger.path).records == 1


def test_rest_baseline_requires_snapshot_and_exhausted_cursor_walk():
    class Client:
        def __init__(self):
            self.cursors = []

        def get_live_orders(self, query):
            assert query == {"product_id": 13}
            return []

        def get_position(self, product_id):
            assert product_id == 13
            return {"size": 0}

        def fills(self, query, page_size, after):
            assert query == {} and page_size == 100
            self.cursors.append(after)
            if after is None:
                return {"success": True, "result": [_rest("fill-1")],
                        "meta": {"after": "next"}}
            return {"success": True, "result": [_rest("fill-2")],
                    "meta": {"after": None}}

    client = Client()
    reader = DeltaPrivateRestBaselineReader(client, product_ids={"BTCUSD": 13})
    baseline = reader.read()
    assert client.cursors == [None, "next"]
    assert baseline.fills_pagination_exhausted
    assert [fill["id"] for fill in baseline.fills] == ["fill-1", "fill-2"]

    with pytest.raises(ValueError, match="page budget exhausted"):
        DeltaPrivateRestBaselineReader(
            Client(), product_ids={"BTCUSD": 13}, max_fill_pages=1,
        ).read()


async def test_observer_needs_socket_snapshots_and_rest_baseline(tmp_path):
    store = DeltaPrivateFillEvidenceStore(
        tmp_path / "fills.jsonl", symbols=("BTCUSD",),
    )
    socket = _client(store)
    socket.connected = True
    socket.connection_generation = 1

    class Reader:
        calls = 0

        def read(self):
            self.calls += 1
            return _baseline([])

    reader = Reader()
    observer = DeltaPrivateTruthObserver(socket, store, reader)
    assert not await observer.reconcile_once()
    assert reader.calls == 0 and not observer.healthy

    socket.ingest({
        "type": "orders", "symbol": "BTCUSD", "action": "snapshot",
        "meta": {"seq_no": 1}, "result": [],
    })
    socket.ingest({
        "type": "positions", "symbol": "BTCUSD", "action": "snapshot",
        "result": [],
    })
    assert await observer.reconcile_once()
    assert reader.calls == 1 and observer.healthy

    # A new commission-absent fill removes readiness until a fresh REST
    # baseline contains that trade ID and binds its commission.
    socket.ingest(_fast("fill-1", 1))
    assert not observer.healthy
    assert not await observer.reconcile_once()
    assert "missing observed" in observer.last_error
    reader.read = lambda: _baseline([_rest("fill-1")])
    assert await observer.reconcile_once()
    assert observer.healthy


async def test_observer_never_carries_health_across_reconnect_generation(tmp_path):
    store = DeltaPrivateFillEvidenceStore(
        tmp_path / "fills.jsonl", symbols=("BTCUSD",),
    )
    socket = _client(store)
    socket.connected = True
    socket.connection_generation = 1
    socket.ingest({
        "type": "orders", "symbol": "BTCUSD", "action": "snapshot",
        "meta": {"seq_no": 1}, "result": [],
    })
    socket.ingest({
        "type": "positions", "symbol": "BTCUSD", "action": "snapshot",
        "result": [],
    })

    class Reader:
        def read(self):
            return _baseline([])

    observer = DeltaPrivateTruthObserver(socket, store, Reader())
    assert await observer.reconcile_once()
    socket.connection_generation = 2
    assert not observer.healthy
