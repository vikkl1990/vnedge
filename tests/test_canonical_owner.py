from __future__ import annotations

from vnedge.exchange.canonical_owner import maintenance_commands


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
