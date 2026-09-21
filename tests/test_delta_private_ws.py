"""Delta private socket evidence cannot quietly become live permission."""

import asyncio
import hashlib
import hmac
import json

import pytest

from vnedge.exchange.delta_private_ws import (
    DeltaPrivateContinuityError,
    DeltaPrivateWsClient,
    key_auth_payload,
)


def collector(events):
    return DeltaPrivateWsClient(
        ("BTC/USD:USD",), api_key="test-key", api_secret="test-secret",
        on_event=events.append,
    )


def test_key_auth_uses_current_signature_and_subscribes_exact_symbols():
    payload = key_auth_payload("test-key", "test-secret", timestamp=1234)
    expected = hmac.new(b"test-secret", b"GET1234/live", hashlib.sha256).hexdigest()
    assert payload == {
        "type": "key-auth",
        "payload": {"api-key": "test-key", "timestamp": 1234, "signature": expected},
    }
    events = []
    client = collector(events)
    assert client.subscribe_payload()["payload"]["channels"] == [
        {"name": name, "symbols": ["BTCUSD"]}
        for name in ("orders", "v2/user_trades", "positions")
    ]
    assert not client.continuity_ok
    with pytest.raises(ValueError, match="reviewed India host"):
        DeltaPrivateWsClient(
            ("BTCUSD",), api_key="k", api_secret="s", on_event=events.append,
            url="wss://socket.delta.exchange",
        )


def test_snapshot_and_order_sequence_gap_fail_closed_without_emitting_gap_event():
    events = []
    client = collector(events)
    client.authenticated = True
    with pytest.raises(DeltaPrivateContinuityError, match="before snapshot"):
        client.ingest({"type": "orders", "symbol": "BTCUSD", "seq_no": 1})
    assert not events

    client = collector(events)
    client.authenticated = True
    first = client.ingest({
        "type": "orders", "symbol": "BTCUSD", "action": "snapshot",
        "meta": {"seq_no": 7}, "result": [],
    })
    assert first is not None and first.sequence == 7
    client.ingest({"type": "orders", "symbol": "BTCUSD", "seq_no": 8})
    with pytest.raises(DeltaPrivateContinuityError, match="8 -> 10"):
        client.ingest({"type": "orders", "symbol": "BTCUSD", "seq_no": 10})
    assert len(events) == 2
    assert not client.continuity_ok
    with pytest.raises(DeltaPrivateContinuityError, match="8 -> 10"):
        client.ingest({"type": "orders", "symbol": "BTCUSD", "seq_no": 11})
    assert len(events) == 2


def test_fast_fill_sequence_and_commission_gap_are_explicit():
    events = []
    client = collector(events)
    client.authenticated = True
    first = client.ingest({
        "type": "v2/user_trades", "sy": "BTCUSD", "se": 4,
        "f": "fill-1", "p": "100", "s": 1,
    })
    assert first is not None and not first.commission_complete
    assert first.sequence == 4
    client.ingest({"type": "v2/user_trades", "sy": "BTCUSD", "se": 5})
    with pytest.raises(DeltaPrivateContinuityError, match="5 -> 1"):
        client.ingest({
            "type": "v2/user_trades", "sy": "BTCUSD", "se": 1,
            "f": "fill-gap", "p": "101", "s": 1,
        })
    assert events[-1].payload["f"] == "fill-gap"
    assert not events[-1].commission_complete
    assert not client.continuity_ok


def test_fast_fill_sink_failure_latches_before_gap_frame_can_be_lost():
    persisted = []

    def persist(event):
        if event.payload.get("f") == "fill-gap":
            raise OSError("durable ledger unavailable")
        persisted.append(event)

    client = DeltaPrivateWsClient(
        ("BTC/USD:USD",), api_key="k", api_secret="s", on_event=persist,
    )
    client.authenticated = True
    client.ingest({
        "type": "v2/user_trades", "sy": "BTCUSD", "se": 4,
        "f": "fill-1", "p": "100", "s": 1,
    })
    with pytest.raises(DeltaPrivateContinuityError, match="persistence failed"):
        client.ingest({
            "type": "v2/user_trades", "sy": "BTCUSD", "se": 6,
            "f": "fill-gap", "p": "101", "s": 1,
        })
    assert [event.payload["f"] for event in persisted] == ["fill-1"]
    assert not client.continuity_ok


def test_private_data_before_auth_and_wrong_symbol_fail_closed():
    events = []
    client = collector(events)
    with pytest.raises(DeltaPrivateContinuityError, match="before key-auth"):
        client.ingest({"type": "positions", "symbol": "BTCUSD", "action": "snapshot"})
    client = collector(events)
    client.authenticated = True
    with pytest.raises(DeltaPrivateContinuityError, match="outside subscribed"):
        client.ingest({"type": "positions", "symbol": "ETHUSD", "action": "snapshot"})
    assert not events


async def test_wire_authenticates_before_subscribing_and_never_sets_readiness():
    events = []
    sent = []
    stop = asyncio.Event()

    class Socket:
        async def send(self, payload):
            sent.append(json.loads(payload))

        async def recv(self):
            if len(sent) == 1:
                return json.dumps({"type": "key-auth", "success": True})
            stop.set()
            return json.dumps({
                "type": "orders", "symbol": "BTCUSD", "action": "snapshot",
                "meta": {"seq_no": 1}, "result": [],
            })

    class Connect:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            return None

    client = DeltaPrivateWsClient(
        ("BTCUSD",), api_key="k", api_secret="s", on_event=events.append,
        connect=lambda url: Connect(), wall_clock=lambda: 1234,
    )
    await client.run_forever(stop_event=stop)
    assert sent[0]["type"] == "key-auth"
    assert sent[1]["type"] == "subscribe"
    assert not client.continuity_ok
