from __future__ import annotations

import asyncio
import json

import pytest

import vnedge.exchange.canonical_owner as owner
from vnedge.exchange.canonical_owner import maintenance_commands


def test_binance_owner_attests_legacy_identity_once(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    class Store:
        def __init__(self, root, *, exchange):
            assert root == tmp_path / "candles"
            assert exchange == "binanceusdm"

        def stamp_legacy_partitions(self, symbol, timeframe, *, source):
            calls.append((symbol, timeframe, source))
            return 2

    monkeypatch.setattr(owner, "CandleParquetStore", Store)
    environment = {
        "VNEDGE_CANONICAL_IDENTITY_MIGRATION_MARKER": str(tmp_path / "state" / "identity.json")
    }

    owner._attest_binance_legacy_identity_once(
        symbols=("BTC/USDT:USDT", "ETH/USDT:USDT"),
        data_root=tmp_path / "data",
        candle_root=tmp_path / "candles",
        environ=environment,
    )
    owner._attest_binance_legacy_identity_once(
        symbols=("BTC/USDT:USDT", "ETH/USDT:USDT"),
        data_root=tmp_path / "data",
        candle_root=tmp_path / "candles",
        environ=environment,
    )

    assert len(calls) == 10
    assert all(source == "canonical_tick_lake" for _, _, source in calls)
    marker = json.loads((tmp_path / "state" / "identity.json").read_text(encoding="utf-8"))
    assert marker["version"] == 2
    assert marker["rows_attested"] == 20


def test_owner_tail_cycle_runs_only_gap_recovery_and_readiness() -> None:
    env = {
        "VISION_BACKFILL_DAYS": "24",
        "SCANNER_PREREQ_SYMBOLS": "BTC/USDT:USDT,ETH/USDT:USDT",
    }
    full = maintenance_commands(env, full=True)
    tail = maintenance_commands(env, full=False)

    assert [command[2] for command in full] == [
        "vnedge.data.aggtrades_backfill",
        "vnedge.data.candle_bootstrap",
        "vnedge.data.binance_gap_recovery",
        "vnedge.data.scanner_prereq",
    ]
    assert [command[2] for command in tail] == [
        "vnedge.data.binance_gap_recovery",
        "vnedge.data.scanner_prereq",
    ]


@pytest.mark.asyncio
async def test_delta_owner_runs_native_recorder_without_binance_maintenance(
    tmp_path, monkeypatch
) -> None:
    calls: dict[str, object] = {}

    class Recorder:
        def __init__(self, symbols, root, **kwargs):
            calls["symbols"] = symbols
            calls["root"] = root
            calls["kwargs"] = kwargs

        async def run(self, *, acquire_writer_lease):
            calls["lease"] = acquire_writer_lease
            await asyncio.sleep(0)

    async def forbidden_maintenance(*args, **kwargs):
        del args, kwargs
        raise AssertionError("Delta owner must not run Binance maintenance")

    monkeypatch.setattr(owner, "DeltaTickRecorder", Recorder)
    monkeypatch.setattr(owner, "_maintenance_loop", forbidden_maintenance)
    async def delta_maintenance(*args, **kwargs):
        calls["maintenance"] = kwargs
        await asyncio.sleep(100)

    monkeypatch.setattr(owner, "_delta_maintenance_loop", delta_maintenance)

    with pytest.raises(RuntimeError, match="Delta canonical recorder exited"):
        await owner.run_owner(
            exchange="delta_india",
            symbols=("BTC/USD:USD", "ETH/USD:USD"),
            data_root=tmp_path / "data",
            candle_root=tmp_path / "candles",
        )

    assert calls["symbols"] == ["BTC/USD:USD", "ETH/USD:USD"]
    assert calls["lease"] is False
    assert calls["kwargs"]["trades_only"] is False
    assert calls["kwargs"]["levels"] == 1
    assert calls["maintenance"]["symbols"] == ("BTC/USD:USD", "ETH/USD:USD")
