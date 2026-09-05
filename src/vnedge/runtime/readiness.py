"""Authoritative runtime readiness split for operator telemetry.

Readiness is deliberately layered: healthy market data is necessary for a
decision, and a healthy decision path is necessary for new execution.  The
object reports permission; it never grants it and never bypasses a gateway.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass


def _unique(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class RuntimeReadiness:
    data_ready: bool
    decision_ready: bool
    parity_ready: bool
    execution_ready: bool
    live_ready: bool
    data_blockers: tuple[str, ...]
    decision_blockers: tuple[str, ...]
    parity_blockers: tuple[str, ...]
    execution_blockers: tuple[str, ...]
    live_blockers: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def build_runtime_readiness(
    *,
    data_blockers: Iterable[str | None] = (),
    decision_blockers: Iterable[str | None] = (),
    parity_blockers: Iterable[str | None] = (),
    execution_blockers: Iterable[str | None] = (),
    live_blockers: Iterable[str | None] = (),
) -> RuntimeReadiness:
    """Build explicit readiness layers without claiming trade authority.

    Shadow execution may be mechanically ready while replay/live approval
    parity is still unproven.  Only ``live_ready`` requires every layer.
    """
    data = _unique(data_blockers)
    decision_local = _unique(decision_blockers)
    parity_local = _unique(parity_blockers)
    execution_local = _unique(execution_blockers)
    live_local = _unique(live_blockers)
    decision = _unique((*data, *decision_local))
    parity = _unique((*decision, *parity_local))
    execution = _unique((*decision, *execution_local))
    live = _unique((*parity, *execution, *live_local))
    return RuntimeReadiness(
        data_ready=not data,
        decision_ready=not decision,
        parity_ready=not parity,
        execution_ready=not execution,
        live_ready=not live,
        data_blockers=data,
        decision_blockers=decision,
        parity_blockers=parity,
        execution_blockers=execution,
        live_blockers=live,
    )
