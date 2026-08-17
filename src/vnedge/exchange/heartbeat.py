"""Transport and market-data liveness for websocket feeds.

Control traffic proves that the socket path is alive. Market traffic proves
that the subscribed application stream is advancing. They are deliberately
tracked separately so a quiet trade tape is not confused with a dead socket,
and a pong-only connection is not treated as tradeable market data.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class HeartbeatStatus(str, Enum):
    OK = "ok"
    NEED_PING = "need_ping"
    PONG_TIMEOUT = "pong_timeout"
    TRANSPORT_STALE = "transport_stale"
    DATA_STALE = "data_stale"


@dataclass(frozen=True, slots=True)
class HeartbeatConfig:
    ping_interval_s: float = 15.0
    pong_timeout_s: float = 10.0
    transport_silence_s: float = 45.0
    data_silence_s: float = 60.0
    use_ws_control_ping: bool = True
    use_app_ping: bool = False

    def __post_init__(self) -> None:
        for name in (
            "ping_interval_s",
            "pong_timeout_s",
            "transport_silence_s",
            "data_silence_s",
        ):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")


class WsHeartbeat:
    """Pure monotonic-clock heartbeat state; socket I/O stays with the caller."""

    def __init__(self, cfg: HeartbeatConfig, *, started_at: float) -> None:
        self.cfg = cfg
        self.started_at = float(started_at)
        self.last_transport_rx: float | None = None
        self.last_market_rx: float | None = None
        self.last_pong: float | None = None
        self.last_ping_sent: float | None = None
        self.awaiting_pong = False

    def reset(self, now: float) -> None:
        self.started_at = float(now)
        self.last_transport_rx = None
        self.last_market_rx = None
        self.last_pong = None
        self.last_ping_sent = None
        self.awaiting_pong = False

    def on_transport_message(self, now: float) -> None:
        self.last_transport_rx = float(now)

    def on_market_message(self, now: float) -> None:
        now = float(now)
        self.last_transport_rx = now
        self.last_market_rx = now

    def on_pong(self, now: float) -> None:
        now = float(now)
        self.last_transport_rx = now
        self.last_pong = now
        self.awaiting_pong = False

    def mark_ping_sent(self, now: float) -> None:
        self.last_ping_sent = float(now)
        self.awaiting_pong = True

    def tick(self, now: float) -> HeartbeatStatus:
        now = float(now)
        if (
            self.awaiting_pong
            and self.last_ping_sent is not None
            and now - self.last_ping_sent > self.cfg.pong_timeout_s
        ):
            return HeartbeatStatus.PONG_TIMEOUT

        transport_base = self.last_transport_rx or self.started_at
        if now - transport_base > self.cfg.transport_silence_s:
            return HeartbeatStatus.TRANSPORT_STALE

        market_base = self.last_market_rx or self.started_at
        if now - market_base > self.cfg.data_silence_s:
            return HeartbeatStatus.DATA_STALE

        ping_base = self.last_ping_sent or self.started_at
        if (
            (self.cfg.use_ws_control_ping or self.cfg.use_app_ping)
            and not self.awaiting_pong
            and now - ping_base >= self.cfg.ping_interval_s
        ):
            return HeartbeatStatus.NEED_PING
        return HeartbeatStatus.OK


@dataclass(slots=True)
class ReconnectBackoff:
    """Bounded exponential reconnect delay with injectable jitter."""

    minimum_s: float = 1.0
    maximum_s: float = 60.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.2
    random_unit: Callable[[], float] = random.random
    _next_s: float = field(init=False)

    def __post_init__(self) -> None:
        if self.minimum_s <= 0 or self.maximum_s < self.minimum_s:
            raise ValueError("invalid reconnect delay bounds")
        if self.multiplier < 1:
            raise ValueError("reconnect multiplier must be at least one")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be in [0, 1]")
        self._next_s = self.minimum_s

    def reset(self) -> None:
        self._next_s = self.minimum_s

    def next_delay(self) -> float:
        base = self._next_s
        self._next_s = min(self.maximum_s, base * self.multiplier)
        unit = min(1.0, max(0.0, float(self.random_unit())))
        jitter = base * self.jitter_ratio * (2.0 * unit - 1.0)
        return max(0.0, min(self.maximum_s, base + jitter))
