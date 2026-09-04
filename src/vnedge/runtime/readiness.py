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
    execution_ready: bool
    data_blockers: tuple[str, ...]
    decision_blockers: tuple[str, ...]
    execution_blockers: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def build_runtime_readiness(
    *,
    data_blockers: Iterable[str | None] = (),
    decision_blockers: Iterable[str | None] = (),
    execution_blockers: Iterable[str | None] = (),
) -> RuntimeReadiness:
    """Build monotonic readiness: execution cannot outrun decision or data."""
    data = _unique(data_blockers)
    decision_local = _unique(decision_blockers)
    execution_local = _unique(execution_blockers)
    decision = _unique((*data, *decision_local))
    execution = _unique((*decision, *execution_local))
    return RuntimeReadiness(
        data_ready=not data,
        decision_ready=not decision,
        execution_ready=not execution,
        data_blockers=data,
        decision_blockers=decision,
        execution_blockers=execution,
    )
