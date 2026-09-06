"""Canonical runtime authority contract.

Historically VNEDGE used ``shadow`` to mean *signal observation* and ``paper``
to mean *live-data simulated execution*.  That made the mode ladder look like
two different trade paths even though only one of them created orders.  This
module separates the two independent questions:

* which clock supplies market events (recorded replay or live), and
* how much execution authority the process has.

The legacy RunnerMode/TradingMode enums remain accepted at their boundaries,
but runtime code consumes this contract.  No strategy is allowed to infer
authority from a lane id, suffix, or dashboard label.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

KERNEL_PATH_ID = "kernel_v1"
PERMISSION_SNAPSHOT_REQUIRED = frozenset({"htf_regime_continuation_15m_v2"})


class DataClock(str, Enum):
    REPLAY = "replay"
    LIVE = "live"


class ExecutionStage(str, Enum):
    """Increasing execution authority.

    OBSERVE produces candidates and evidence only.  SHADOW uses the canonical
    order path with a simulated adapter.  LIVE_* uses a real venue adapter and
    remains protected by the existing three live gates.
    """

    OBSERVE = "observe"
    SHADOW = "shadow_execution"
    LIVE_SMALL = "live_small"
    LIVE_FULL = "live_full"
    EMERGENCY_REDUCE_ONLY = "emergency_reduce_only"

    @property
    def can_submit_orders(self) -> bool:
        return self is not ExecutionStage.OBSERVE

    @property
    def sends_real_orders(self) -> bool:
        return self in {
            ExecutionStage.LIVE_SMALL,
            ExecutionStage.LIVE_FULL,
            ExecutionStage.EMERGENCY_REDUCE_ONLY,
        }


class AdapterKind(str, Enum):
    SIMULATED = "simulated"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    clock: DataClock
    stage: ExecutionStage

    @classmethod
    def from_runner_mode(
        cls,
        mode: object,
        *,
        live_clock: bool = True,
    ) -> ExecutionContext:
        """Map the legacy paper/shadow runner names to their real authority.

        Legacy ``shadow`` and ``paper`` now share the same simulated execution
        authority.  Their operator labels differ, but neither may fork around
        the canonical order manager / journal path.
        """

        value = str(getattr(mode, "value", mode)).lower()
        if value in {"shadow", "paper"}:
            stage = ExecutionStage.SHADOW
        else:
            raise ValueError(f"unsupported runner mode {value!r}")
        return cls(DataClock.LIVE if live_clock else DataClock.REPLAY, stage)

    @classmethod
    def from_trading_mode(cls, mode: object) -> ExecutionContext:
        value = str(getattr(mode, "value", mode)).lower()
        try:
            stage = ExecutionStage(value)
        except ValueError as exc:
            raise ValueError(f"trading mode {value!r} has no execution authority") from exc
        return cls(DataClock.LIVE, stage)

    def validate_adapter(self, adapter_kind: AdapterKind) -> None:
        """Fail closed when authority and adapter type disagree."""

        if not self.stage.can_submit_orders:
            if adapter_kind is AdapterKind.LIVE:
                raise RuntimeError("observe stage can never hold a live execution adapter")
            return
        if self.stage.sends_real_orders and adapter_kind is not AdapterKind.LIVE:
            raise RuntimeError(f"{self.stage.value} requires a live execution adapter")
        if not self.stage.sends_real_orders and adapter_kind is not AdapterKind.SIMULATED:
            raise RuntimeError("shadow execution requires a simulated execution adapter")
