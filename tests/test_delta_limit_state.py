import json

import pytest

from vnedge.exchange.delta_limit_state import (
    DeltaRestBudget,
    WsConnectBreaker,
    delta_rest_weight,
    parse_rate_limit_reset,
)


def test_documented_delta_rest_weights_and_fixed_window():
    assert delta_rest_weight("GET", "/v2/products/BTCUSD") == 3
    assert delta_rest_weight("POST", "/v2/orders") == 5
    assert delta_rest_weight("POST", "/v2/orders/batch") == 25
    budget = DeltaRestBudget()
    assert budget.reserve("GET", "/v2/products/BTCUSD", now=10.0) == 0
    assert budget.used == 3
    budget.exhausted = True
    assert budget.reserve("GET", "/v2/products/ETHUSD", now=11.0) == 299.0


def test_rate_limit_reset_parses_epoch_ms_and_retry_after():
    assert parse_rate_limit_reset({"X-RATE-LIMIT-RESET": "2000000000000"}, now=1.0) == 2_000_000_000.0
    assert parse_rate_limit_reset({"Retry-After": "7"}, now=10.0) == 17.0


def test_ws_429_breaker_persists_across_restarts(tmp_path):
    path = tmp_path / "breaker.json"
    first = WsConnectBreaker(path)
    first.on_handshake_429(100.0)
    saved = json.loads(path.read_text())
    assert saved["cooldown_until"] == 700.0
    second = WsConnectBreaker(path)
    second.load()
    with pytest.raises(RuntimeError, match="cooldown"):
        second.allow_connect(200.0)
    second.allow_connect(701.0)
