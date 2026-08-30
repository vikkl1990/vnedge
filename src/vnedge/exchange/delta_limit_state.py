"""Persistent Delta websocket breaker and weighted REST budget."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DELTA_REST_BUDGET = 10_000
DELTA_REST_WINDOW_S = 300.0
DELTA_WS_HANDSHAKE_COOLDOWN_S = 600.0


def parse_rate_limit_reset(headers: Mapping[str, str], *, now: float | None = None) -> float:
    current = time.time() if now is None else float(now)
    raw = headers.get("X-RATE-LIMIT-RESET") or headers.get("x-rate-limit-reset")
    if raw:
        value = float(raw)
        if value > 1e12:
            return value / 1000.0
        if value > current * 10:
            return value / 1000.0
        return value
    retry = headers.get("Retry-After") or headers.get("retry-after")
    if retry:
        return current + float(retry)
    return current + DELTA_REST_WINDOW_S


@dataclass(slots=True)
class WsConnectBreaker:
    path: Path
    cooldown_until: float = 0.0
    handshake_429s: int = 0

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.cooldown_until = float(raw.get("cooldown_until", 0.0))
        self.handshake_429s = int(raw.get("handshake_429s", 0))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({
                "cooldown_until": self.cooldown_until,
                "handshake_429s": self.handshake_429s,
            }),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def remaining(self, now: float) -> float:
        return max(0.0, self.cooldown_until - float(now))

    def allow_connect(self, now: float) -> None:
        wait = self.remaining(now)
        if wait > 0:
            raise RuntimeError(f"Delta websocket cooldown active for {wait:.1f}s")

    def on_handshake_429(self, now: float, *, reset_at: float | None = None) -> None:
        wait = DELTA_WS_HANDSHAKE_COOLDOWN_S
        if reset_at is not None:
            wait = max(300.0, min(600.0, reset_at - now + 1.0))
        self.handshake_429s += 1
        self.cooldown_until = now + wait
        self.save()


def delta_rest_weight(method: str, path: str) -> int:
    method, path = method.upper(), path.split("?", 1)[0]
    explicit = {
        ("GET", "/v2/products"): 3,
        ("GET", "/v2/tickers"): 3,
        ("GET", "/v2/l2orderbook"): 3,
        ("GET", "/v2/history/candles"): 3,
        ("GET", "/v2/rate_limits/quota"): 1,
        ("POST", "/v2/orders"): 5,
        ("PUT", "/v2/orders"): 5,
        ("DELETE", "/v2/orders"): 5,
        ("GET", "/v2/orders/history"): 10,
        ("GET", "/v2/fills"): 10,
        ("POST", "/v2/orders/batch"): 25,
    }
    if (method, path) in explicit:
        return explicit[(method, path)]
    if method == "GET" and path.startswith("/v2/products/"):
        return 3
    return 1


@dataclass(slots=True)
class DeltaRestBudget:
    window_started_at: float = 0.0
    used: int = 0
    reset_at: float = 0.0
    exhausted: bool = False

    def reserve(self, method: str, path: str, *, now: float) -> float:
        if self.window_started_at == 0.0 or now >= self.reset_at:
            self.window_started_at = now
            self.reset_at = now + DELTA_REST_WINDOW_S
            self.used = 0
            self.exhausted = False
        weight = delta_rest_weight(method, path)
        if self.exhausted or self.used + weight > DELTA_REST_BUDGET:
            return max(0.0, self.reset_at - now)
        self.used += weight
        return 0.0

    def note_429(self, headers: Mapping[str, str], *, now: float) -> float:
        self.exhausted = True
        self.reset_at = parse_rate_limit_reset(headers, now=now)
        return max(0.0, self.reset_at - now)
