import pytest

from vnedge.research.replay_cost_schema import (
    BOOKED_EXECUTION,
    LEGACY_GATE_NET,
    read_closed_replay_net,
    require_comparable_replay_nets,
)


def test_schema_two_reads_booked_execution_and_gate_separately() -> None:
    view = read_closed_replay_net(
        {"net_bps": 24.2, "net_execution_bps": 24.2, "net_gate_bps": 21.2},
        artifact_schema_version=2,
    )

    assert view.pnl_bps == pytest.approx(24.2)
    assert view.gate_check_bps == pytest.approx(21.2)
    assert view.semantics == BOOKED_EXECUTION
    assert view.legacy is False


def test_explicit_execution_wins_during_schema_one_migration() -> None:
    view = read_closed_replay_net(
        {"net_bps": 21.2, "net_execution_bps": 24.2, "net_gate_bps": 21.2},
        artifact_schema_version=1,
    )

    assert view.pnl_bps == pytest.approx(24.2)
    assert view.gate_check_bps == pytest.approx(21.2)
    assert view.semantics == BOOKED_EXECUTION
    assert view.legacy is False


def test_schema_one_alias_is_readable_but_labeled_gate_net() -> None:
    view = read_closed_replay_net(
        {"net_bps": 0.0}, artifact_schema_version=1
    )

    assert view.pnl_bps == 0.0
    assert view.gate_check_bps == 0.0
    assert view.semantics == LEGACY_GATE_NET
    assert view.legacy is True


def test_mixed_legacy_and_booked_rankings_fail_closed() -> None:
    legacy = read_closed_replay_net({"net_bps": 21.2}, artifact_schema_version=1)
    booked = read_closed_replay_net({"net_bps": 24.2}, artifact_schema_version=2)

    with pytest.raises(ValueError, match="mixed replay net semantics"):
        require_comparable_replay_nets([legacy, booked])


def test_same_semantics_are_comparable() -> None:
    first = read_closed_replay_net(
        {"net_execution_bps": 24.2}, artifact_schema_version=1
    )
    second = read_closed_replay_net({"net_bps": 10.0}, artifact_schema_version=2)

    assert require_comparable_replay_nets([first, second]) == BOOKED_EXECUTION
