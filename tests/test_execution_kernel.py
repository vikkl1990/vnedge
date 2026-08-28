from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from vnedge.execution.order_manager import OrderManager
from vnedge.risk.risk_manager import AccountState, MarketState, OrderIntent
from vnedge.runtime.execution_contract import (
    AdapterKind,
    DataClock,
    ExecutionContext,
    ExecutionStage,
)
from vnedge.runtime.execution_kernel import ExecutionKernel
from vnedge.runtime.runner_config import RunnerMode


class _RecordingOrderManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = object()

    async def submit(self, intent, account, market, intent_key, now=None, *, replaces=None):
        self.calls.append(
            {
                "intent": intent,
                "account": account,
                "market": market,
                "intent_key": intent_key,
                "now": now,
                "replaces": replaces,
            }
        )
        return self.result


def _intent() -> OrderIntent:
    return OrderIntent(
        symbol="BTC/USDT:USDT",
        side="long",
        quantity=0.001,
        notional_usd=60.0,
        leverage=2.0,
        strategy_id="test",
    )


def _account() -> AccountState:
    return AccountState(
        equity_usd=1000.0,
        daily_pnl_usd=0.0,
        peak_equity_usd=1000.0,
        consecutive_losses=0,
        open_positions=0,
    )


def _market() -> MarketState:
    return MarketState(
        symbol="BTC/USDT:USDT",
        last_update=datetime(2026, 8, 28, tzinfo=UTC),
        spread_bps=0.34,
        estimated_slippage_bps=0.5,
        funding_rate=0.0,
        exchange_healthy=True,
    )


def test_legacy_runner_names_map_to_actual_authority() -> None:
    observe = ExecutionContext.from_runner_mode(RunnerMode.SHADOW)
    paper = ExecutionContext.from_runner_mode(RunnerMode.PAPER)

    assert observe == ExecutionContext(DataClock.LIVE, ExecutionStage.OBSERVE)
    assert paper == ExecutionContext(DataClock.LIVE, ExecutionStage.SHADOW)


def test_adapter_authority_mismatch_fails_closed() -> None:
    manager = cast(OrderManager, _RecordingOrderManager())
    with pytest.raises(RuntimeError, match="requires a live"):
        ExecutionKernel(
            ExecutionContext(DataClock.LIVE, ExecutionStage.LIVE_SMALL),
            manager,
            AdapterKind.SIMULATED,
        )
    with pytest.raises(RuntimeError, match="simulated"):
        ExecutionKernel(
            ExecutionContext(DataClock.LIVE, ExecutionStage.SHADOW),
            manager,
            AdapterKind.LIVE,
        )


async def test_observe_never_reaches_order_manager() -> None:
    manager = _RecordingOrderManager()
    kernel = ExecutionKernel(
        ExecutionContext(DataClock.LIVE, ExecutionStage.OBSERVE),
        cast(OrderManager, manager),
        AdapterKind.SIMULATED,
    )
    with pytest.raises(PermissionError, match="observe stage"):
        await kernel.submit(_intent(), _account(), _market(), "k")
    assert manager.calls == []


async def test_emergency_stage_only_forwards_reduce_only_intents() -> None:
    manager = _RecordingOrderManager()
    kernel = ExecutionKernel(
        ExecutionContext(DataClock.LIVE, ExecutionStage.EMERGENCY_REDUCE_ONLY),
        cast(OrderManager, manager),
        AdapterKind.LIVE,
    )

    with pytest.raises(PermissionError, match="reduce-only"):
        await kernel.submit(_intent(), _account(), _market(), "entry")
    assert manager.calls == []

    exit_intent = replace(_intent(), side="short", reduce_only=True)
    await kernel.submit(exit_intent, _account(), _market(), "exit")
    assert len(manager.calls) == 1


@pytest.mark.parametrize(
    ("stage", "adapter"),
    [
        (ExecutionStage.SHADOW, AdapterKind.SIMULATED),
        (ExecutionStage.LIVE_SMALL, AdapterKind.LIVE),
        (ExecutionStage.LIVE_FULL, AdapterKind.LIVE),
    ],
)
async def test_shadow_and_live_forward_through_identical_kernel(
    stage: ExecutionStage, adapter: AdapterKind
) -> None:
    manager = _RecordingOrderManager()
    kernel = ExecutionKernel(
        ExecutionContext(DataClock.LIVE, stage),
        cast(OrderManager, manager),
        adapter,
    )
    now = datetime(2026, 8, 28, tzinfo=UTC)
    result = await kernel.submit(
        _intent(), _account(), _market(), "stable-key", now=now
    )

    assert result is manager.result
    assert len(manager.calls) == 1
    assert manager.calls[0]["intent_key"] == "stable-key"
    assert manager.calls[0]["now"] == now
