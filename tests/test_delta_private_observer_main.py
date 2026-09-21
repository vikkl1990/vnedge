"""The Delta private observer remains read-only and explicitly configured."""

import asyncio

import pytest

from vnedge.runtime.delta_private_observer_main import (
    DeltaPrivateObserverConfig,
    parse_product_ids,
    run_observer,
)


def test_product_id_parser_freezes_exact_native_roster():
    assert parse_product_ids("BTCUSD:13,ETHUSD:27") == {
        "BTCUSD": 13,
        "ETHUSD": 27,
    }
    for invalid in ("", "BTCUSD", "BTCUSD:13,BTCUSD:14"):
        with pytest.raises(ValueError):
            parse_product_ids(invalid)


def test_observer_config_refuses_unmapped_or_invalid_products(tmp_path):
    with pytest.raises(ValueError, match="exactly match"):
        DeltaPrivateObserverConfig(
            symbols=("BTCUSD", "ETHUSD"),
            product_ids={"BTCUSD": 13},
            ledger_path=tmp_path / "fills.jsonl",
        )
    with pytest.raises(ValueError, match="positive"):
        DeltaPrivateObserverConfig(
            symbols=("BTCUSD",),
            product_ids={"BTCUSD": 0},
            ledger_path=tmp_path / "fills.jsonl",
        )


async def test_observer_refuses_before_constructing_clients_without_credentials(tmp_path):
    constructed = []
    config = DeltaPrivateObserverConfig(
        symbols=("BTCUSD",),
        product_ids={"BTCUSD": 13},
        ledger_path=tmp_path / "fills.jsonl",
    )

    def forbidden_factory(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("venue client must not be constructed")

    with pytest.raises(ValueError, match="credentials are required"):
        await run_observer(
            config,
            api_key="",
            api_secret="",
            rest_client_factory=forbidden_factory,
            socket_factory=forbidden_factory,
        )
    assert constructed == []


async def test_observer_runner_constructs_only_read_evidence_components(tmp_path):
    config = DeltaPrivateObserverConfig(
        symbols=("BTCUSD",),
        product_ids={"BTCUSD": 13},
        ledger_path=tmp_path / "fills.jsonl",
    )
    captured = {}

    class Socket:
        connected = False
        authenticated = False
        faulted = False
        snapshots_complete = False
        continuity_ok = False
        connection_generation = 0

        def __init__(self, symbols, **kwargs):
            self.symbols = symbols
            captured["socket_kwargs"] = kwargs

        async def run_forever(self, *, stop_event):
            await stop_event.wait()

    class Rest:
        def __init__(self, **kwargs):
            captured["rest_kwargs"] = kwargs

    stop = asyncio.Event()
    stop.set()
    observer = await run_observer(
        config,
        api_key="read-key",
        api_secret="read-secret",
        stop_event=stop,
        rest_client_factory=Rest,
        socket_factory=Socket,
    )
    assert observer.evidence.trade_ids == frozenset()
    assert captured["rest_kwargs"]["base_url"].endswith("india.delta.exchange")
    assert captured["socket_kwargs"]["on_event"] == observer.evidence.record_event
    assert not hasattr(observer, "submit")
