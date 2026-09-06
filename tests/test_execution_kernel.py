from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from vnedge.execution.evidence import DecisionEnvelope, ExecutionEvidence
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
from vnedge.strategy.arm_evidence import FrozenPermissionSnapshot, ImmutableBarRef


class _RecordingOrderManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = object()
        self.evaluate_calls: list[dict[str, Any]] = []

    def evaluate_candidate(self, intent, account, market, *, now=None, evidence=None):
        self.evaluate_calls.append(
            {"intent": intent, "account": account, "market": market, "now": now,
             "evidence": evidence}
        )
        return self.result

    async def submit(
        self, intent, account, market, intent_key=None, now=None, *, replaces=None,
        evidence=None,
    ):
        self.calls.append(
            {
                "intent": intent,
                "account": account,
                "market": market,
                "intent_key": intent_key,
                "evidence": evidence,
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
    )


def _evidence(
    *, strategy_id: str = "test_v1", snapshot_id: str | None = None,
    permission_snapshot: FrozenPermissionSnapshot | None = None,
    side: str = "long",
) -> ExecutionEvidence:
    if (
        strategy_id == "htf_regime_continuation_15m_v2"
        and permission_snapshot is None
    ):
        return ExecutionEvidence.create(
            strategy_id=strategy_id,
            symbol="BTC/USDT:USDT",
            timeframe="15m",
            bar_open=datetime(2026, 8, 28, tzinfo=UTC),
            side=side,
            htf_snapshot_id=snapshot_id,
            permission_snapshot=None,
            candle_source="parquet",
            entry_clock="next_15m_open",
        )
    snapshot = permission_snapshot or FrozenPermissionSnapshot(
        decision_bar=ImmutableBarRef(
            timeframe="15m",
            open_time=datetime(2026, 8, 28, tzinfo=UTC),
            close_time=datetime(2026, 8, 28, 0, 15, tzinfo=UTC),
            source="canonical_tick_lake",
            content_sha256="f" * 64,
        ),
        context_bars=(),
        allow_long=side == "long",
        allow_short=side == "short",
        regime_state="not_applicable",
        direction="not_applicable",
        reason="test",
    )
    if snapshot_id is not None and snapshot_id != snapshot.snapshot_id:
        # Preserve the malformed-evidence fixture behavior for validation tests.
        return ExecutionEvidence.create(
            strategy_id=strategy_id,
            symbol="BTC/USDT:USDT",
            timeframe="15m",
            bar_open=datetime(2026, 8, 28, tzinfo=UTC),
            side=side,
            htf_snapshot_id=snapshot_id,
            permission_snapshot=permission_snapshot,
            candle_source="parquet",
            entry_clock="next_15m_open",
        )
    decision = DecisionEnvelope.create(
        strategy_id=strategy_id,
        symbol="BTC/USDT:USDT",
        timeframe="15m",
        side=side,
        permission_snapshot=snapshot,
        entry_clock="next_15m_open",
    )
    return ExecutionEvidence.from_decision(decision)


def _permission_snapshot() -> FrozenPermissionSnapshot:
    return FrozenPermissionSnapshot(
        decision_bar=ImmutableBarRef(
            timeframe="15m",
            open_time=datetime(2026, 8, 28, tzinfo=UTC),
            close_time=datetime(2026, 8, 28, 0, 15, tzinfo=UTC),
            source="canonical_tick_lake",
            content_sha256="a" * 64,
        ),
        context_bars=(
            ImmutableBarRef(
                timeframe="4h",
                open_time=datetime(2026, 8, 27, 20, tzinfo=UTC),
                close_time=datetime(2026, 8, 28, tzinfo=UTC),
                source="canonical_tick_lake",
                content_sha256="b" * 64,
            ),
            ImmutableBarRef(
                timeframe="1d",
                open_time=datetime(2026, 8, 27, tzinfo=UTC),
                close_time=datetime(2026, 8, 28, tzinfo=UTC),
                source="canonical_tick_lake",
                content_sha256="c" * 64,
            ),
        ),
        allow_long=True,
        allow_short=False,
        regime_state="continuation",
        direction="long",
        reason="bound_htf_context",
        regime_version="market_regime_v2",
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
    shadow = ExecutionContext.from_runner_mode(RunnerMode.SHADOW)
    paper = ExecutionContext.from_runner_mode(RunnerMode.PAPER)

    assert shadow == ExecutionContext(DataClock.LIVE, ExecutionStage.SHADOW)
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
        await kernel.submit(_intent(), _account(), _market(), evidence=_evidence())
    assert manager.calls == []


async def test_htf_v2_cannot_submit_without_frozen_permission() -> None:
    manager = _RecordingOrderManager()
    kernel = ExecutionKernel(
        ExecutionContext(DataClock.LIVE, ExecutionStage.SHADOW),
        cast(OrderManager, manager),
        AdapterKind.SIMULATED,
    )
    with pytest.raises(PermissionError, match="requires a frozen permission snapshot"):
        await kernel.submit(
            _intent(), _account(), _market(),
            evidence=_evidence(strategy_id="htf_regime_continuation_15m_v2"),
        )

    assert manager.calls == []


async def test_htf_v2_submits_with_sourced_frozen_permission() -> None:
    manager = _RecordingOrderManager()
    kernel = ExecutionKernel(
        ExecutionContext(DataClock.LIVE, ExecutionStage.SHADOW),
        cast(OrderManager, manager),
        AdapterKind.SIMULATED,
    )
    snapshot = _permission_snapshot()

    await kernel.submit(
        _intent(),
        _account(),
        _market(),
        evidence=_evidence(
            strategy_id="htf_regime_continuation_15m_v2",
            snapshot_id=snapshot.snapshot_id,
            permission_snapshot=snapshot,
        ),
    )

    assert manager.calls[0]["evidence"].permission_snapshot == snapshot


def test_observe_candidate_uses_order_manager_risk_boundary_without_submit() -> None:
    manager = _RecordingOrderManager()
    kernel = ExecutionKernel(
        ExecutionContext(DataClock.LIVE, ExecutionStage.OBSERVE),
        cast(OrderManager, manager),
        AdapterKind.SIMULATED,
    )
    now = datetime(2026, 8, 28, tzinfo=UTC)
    evidence = _evidence()
    result = kernel.evaluate_candidate(
        _intent(), _account(), _market(), evidence=evidence, now=now
    )
    assert result is manager.result
    assert manager.calls == []
    assert manager.evaluate_calls == [
        {"intent": _intent(), "account": _account(), "market": _market(), "now": now,
         "evidence": evidence}
    ]


async def test_emergency_stage_only_forwards_reduce_only_intents() -> None:
    manager = _RecordingOrderManager()
    kernel = ExecutionKernel(
        ExecutionContext(DataClock.LIVE, ExecutionStage.EMERGENCY_REDUCE_ONLY),
        cast(OrderManager, manager),
        AdapterKind.LIVE,
    )

    with pytest.raises(PermissionError, match="reduce-only"):
        await kernel.submit(_intent(), _account(), _market(), evidence=_evidence())
    assert manager.calls == []

    exit_intent = replace(_intent(), side="short", reduce_only=True)
    await kernel.submit(
        exit_intent, _account(), _market(), evidence=_evidence(side="short")
    )
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
        _intent(), _account(), _market(), evidence=_evidence(), now=now
    )

    assert result is manager.result
    assert len(manager.calls) == 1
    assert manager.calls[0]["intent_key"] is None
    assert manager.calls[0]["evidence"] == _evidence()
    assert manager.calls[0]["now"] == now
