"""Version-aware interpretation of closed-bar replay cost fields.

The compatibility name ``net_bps`` changed meaning in scanner replay schema 2:
schema 1 used the conservative CostGate wall, while schema 2 uses booked
execution cost.  Consumers must use this module instead of treating the alias
as an unversioned performance number.

Quote replays are deliberately outside this contract: without fills, funding,
and the live approval path they are not performance evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

BOOKED_EXECUTION = "booked_execution"
LEGACY_GATE_NET = "legacy_gate_net"


@dataclass(frozen=True, slots=True)
class ReplayNetView:
    """Normalized replay totals plus the provenance needed for safe ranking."""

    schema_version: int
    pnl_bps: float | None
    gate_check_bps: float | None
    semantics: str
    legacy: bool


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_closed_replay_net(
    row: Mapping[str, Any],
    *,
    artifact_schema_version: int | None = None,
) -> ReplayNetView:
    """Read one closed-bar replay result without guessing ``net_bps``.

    Explicit ``net_execution_bps`` always wins, including in a schema-1
    artifact produced during the migration.  A schema-1 row with only
    ``net_bps`` remains readable, but is marked legacy gate-net so callers can
    display it without mixing it into a booked-execution ranking.
    """

    raw_version = row.get("schema_version", artifact_schema_version or 1)
    try:
        schema_version = int(raw_version)
    except (TypeError, ValueError):
        schema_version = 1

    explicit_execution = _number(row.get("net_execution_bps"))
    compatibility_net = _number(row.get("net_bps"))
    explicit_gate = _number(row.get("net_gate_bps"))

    if explicit_execution is not None:
        return ReplayNetView(
            schema_version=schema_version,
            pnl_bps=explicit_execution,
            gate_check_bps=explicit_gate,
            semantics=BOOKED_EXECUTION,
            legacy=False,
        )
    if schema_version >= 2:
        return ReplayNetView(
            schema_version=schema_version,
            pnl_bps=compatibility_net,
            gate_check_bps=explicit_gate,
            semantics=BOOKED_EXECUTION,
            legacy=False,
        )
    return ReplayNetView(
        schema_version=schema_version,
        pnl_bps=compatibility_net,
        gate_check_bps=(
            explicit_gate if explicit_gate is not None else compatibility_net
        ),
        semantics=LEGACY_GATE_NET,
        legacy=True,
    )


def require_comparable_replay_nets(views: Iterable[ReplayNetView]) -> str | None:
    """Reject a ranking that mixes legacy gate-net with booked execution-net."""

    semantics = {view.semantics for view in views if view.pnl_bps is not None}
    if len(semantics) > 1:
        raise ValueError(
            "mixed replay net semantics: schema-1 gate-net and schema-2 booked "
            "execution-net must be displayed separately"
        )
    return next(iter(semantics), None)


__all__ = [
    "BOOKED_EXECUTION",
    "LEGACY_GATE_NET",
    "ReplayNetView",
    "read_closed_replay_net",
    "require_comparable_replay_nets",
]
