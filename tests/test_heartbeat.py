"""Dual websocket heartbeat and reconnect policy tests."""

import pytest

from vnedge.exchange.heartbeat import (
    HeartbeatConfig,
    HeartbeatStatus,
    ReconnectBackoff,
    WsHeartbeat,
)


def test_control_pong_does_not_make_stale_market_data_healthy() -> None:
    hb = WsHeartbeat(
        HeartbeatConfig(
            ping_interval_s=5,
            pong_timeout_s=2,
            transport_silence_s=20,
            data_silence_s=10,
        ),
        started_at=0,
    )
    hb.on_pong(9)
    assert hb.tick(11) == HeartbeatStatus.DATA_STALE


def test_market_message_proves_transport_and_application_liveness() -> None:
    hb = WsHeartbeat(
        HeartbeatConfig(
            ping_interval_s=15,
            pong_timeout_s=5,
            transport_silence_s=30,
            data_silence_s=20,
        ),
        started_at=0,
    )
    hb.on_market_message(10)
    assert hb.tick(14) == HeartbeatStatus.OK
    assert hb.last_transport_rx == hb.last_market_rx == 10


def test_ping_deadline_and_transport_silence_are_distinct() -> None:
    cfg = HeartbeatConfig(
        ping_interval_s=5,
        pong_timeout_s=2,
        transport_silence_s=10,
        data_silence_s=20,
    )
    hb = WsHeartbeat(cfg, started_at=0)
    assert hb.tick(5) == HeartbeatStatus.NEED_PING
    hb.mark_ping_sent(5)
    assert hb.tick(7.1) == HeartbeatStatus.PONG_TIMEOUT

    hb.reset(20)
    assert hb.tick(30.1) == HeartbeatStatus.TRANSPORT_STALE


def test_reconnect_backoff_is_bounded_and_resettable() -> None:
    backoff = ReconnectBackoff(
        minimum_s=1,
        maximum_s=5,
        multiplier=2,
        jitter_ratio=0.2,
        random_unit=lambda: 0.5,
    )
    assert [backoff.next_delay() for _ in range(5)] == [1, 2, 4, 5, 5]
    backoff.reset()
    assert backoff.next_delay() == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ping_interval_s": 0},
        {"pong_timeout_s": 0},
        {"transport_silence_s": -1},
        {"data_silence_s": 0},
    ],
)
def test_heartbeat_config_rejects_non_positive_timeouts(kwargs) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        HeartbeatConfig(**kwargs)
