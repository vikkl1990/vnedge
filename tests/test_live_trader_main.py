"""Live-trader entrypoint — the gate chain must refuse (and build NO live client)
unless all three gates + the fail-closed checklist + credentials are satisfied."""

import asyncio

import pytest

from vnedge.config.settings import Settings
from vnedge.data.schemas import normalize_candles
from vnedge.runtime.live_trader_main import (
    _EXIT_CHECKLIST,
    _EXIT_GATES,
    _EXIT_OK,
    _EXIT_STRATEGY,
    LiveTraderRunConfig,
    _default_account,
    _default_adapter,
    _default_warmup,
    _timeframe_milliseconds,
    run_live_trader,
)
from vnedge.strategy.base_strategy import BaseStrategy

BASE = 1_750_000_000_000
HOUR = 3_600_000
LIVE_ENV = {
    "trading_mode": "live_small",
    "live_trading_enabled": True,
    "confirm_live_trading": "I_UNDERSTAND_THIS_IS_HIGH_RISK",
}
CFG = LiveTraderRunConfig(
    exchange="binanceusdm",
    symbol="BTC/USDT:USDT",
    timeframe="1h",
    strategy_id="trend_continuation_v1",
)


class Fac:
    """Records whether it was called; returns a fixed object."""

    def __init__(self, obj):
        self.obj = obj
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.obj


class FakeAdapter:
    async def close(self):
        pass


class FakeAccount:
    pass


class FakeFeed:
    def __init__(self):
        self.closed_candles = asyncio.Queue()
        self.started = self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeStrategy(BaseStrategy):
    strategy_id = "funding_mean_reversion_v1"
    warmup_bars = 2

    def prepare(self, candles):
        return candles.copy()

    def signal(self, df, index):
        return None


class FakeStream:
    def __init__(self):
        self.closed = False

    async def run_forever(self, *, symbol=None, stop_event=None, retry_delay_seconds=1.0):
        if stop_event is not None:
            await stop_event.wait()

    async def close(self):
        self.closed = True


def _warmup():
    async def load(config, bars):
        return normalize_candles(
            [[BASE + i * HOUR, 100.0, 101.0, 99.0, 100.0, 10.0] for i in range(5)]
        )
    return load


def _live_env(monkeypatch, tmp_path):
    # The same setup the drill's happy path uses to CLEAR the pre-live checklist.
    monkeypatch.setenv("VNEDGE_EXEC_API_KEY", "k")
    monkeypatch.setenv("VNEDGE_EXEC_API_SECRET", "s")
    monkeypatch.setenv("PRE_LIVE_LADDER_ATTESTED", "1")
    monkeypatch.setenv("KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.setenv("DECISION_JOURNAL", str(tmp_path / "dj.jsonl"))
    monkeypatch.chdir(tmp_path)


def _facs():
    return {
        "adapter_factory": Fac(FakeAdapter()),
        "feed_factory": Fac(FakeFeed()),
        "account_factory": Fac(FakeAccount()),
        "strategy_factory": Fac(FakeStrategy()),
        "warmup_loader": _warmup(),
        # M2: a fake private stream so the happy path doesn't construct a real venue
        # client (the default fill ledger writes under the chdir'd tmp_path).
        "private_stream_factory": Fac(FakeStream()),
    }


async def test_refuses_without_three_gates_and_builds_no_live_client(tmp_path, monkeypatch):
    _live_env(monkeypatch, tmp_path)
    facs = _facs()
    code = await run_live_trader(Settings(), CFG, max_bars=0, **facs)  # backtest — gates closed
    assert code == _EXIT_GATES
    # the whole point: NOT ONE live client is constructed when gated out
    assert facs["adapter_factory"].calls == 0
    assert facs["feed_factory"].calls == 0
    assert facs["account_factory"].calls == 0


async def test_refuses_when_checklist_not_cleared(tmp_path, monkeypatch):
    _live_env(monkeypatch, tmp_path)
    monkeypatch.delenv("PRE_LIVE_LADDER_ATTESTED")  # ladder not attested -> checklist fails
    facs = _facs()
    code = await run_live_trader(Settings(**LIVE_ENV), CFG, max_bars=0, **facs)
    assert code == _EXIT_CHECKLIST
    assert facs["adapter_factory"].calls == 0  # still no live client


async def test_killed_strategy_is_refused_before_any_live_client(tmp_path, monkeypatch):
    _live_env(monkeypatch, tmp_path)
    facs = _facs()
    killed = LiveTraderRunConfig(
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        strategy_id="funding_mean_reversion_v1",
    )
    code = await run_live_trader(Settings(**LIVE_ENV), killed, max_bars=0, **facs)
    assert code == _EXIT_STRATEGY
    assert facs["adapter_factory"].calls == 0


async def test_all_gates_open_wires_and_runs(tmp_path, monkeypatch):
    from vnedge.strategy import strategy_registry

    _live_env(monkeypatch, tmp_path)
    # Exercise the post-approval wiring path without changing the production
    # default, whose capital allowlist is deliberately empty.
    monkeypatch.setattr(strategy_registry, "CAPITAL_APPROVED", frozenset({CFG.strategy_id}))
    facs = _facs()
    code = await run_live_trader(Settings(**LIVE_ENV), CFG, max_bars=0, **facs)
    assert code == _EXIT_OK
    # gates open -> every live component is wired, and the feed is started + stopped
    assert facs["adapter_factory"].calls == 1
    assert facs["feed_factory"].calls == 1
    assert facs["account_factory"].calls == 1
    assert facs["strategy_factory"].calls == 1
    assert facs["feed_factory"].obj.started and facs["feed_factory"].obj.stopped
    # M2: the private fill/order stream is wired and torn down with the session
    assert facs["private_stream_factory"].calls == 1
    assert facs["private_stream_factory"].obj.closed


def test_timeframe_conversion_is_explicit_and_validated():
    assert _timeframe_milliseconds("15m") == 900_000
    assert _timeframe_milliseconds("4h") == 14_400_000
    with pytest.raises(ValueError):
        _timeframe_milliseconds("weekly")


async def test_default_warmup_uses_since_and_until_not_limit(monkeypatch):
    from vnedge.data import ccxt_client

    calls = []

    class FakeRest:
        def __init__(self, exchange):
            assert exchange == "binanceusdm"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def fetch_candles(self, symbol, timeframe, **kwargs):
            calls.append((symbol, timeframe, kwargs))
            return [[BASE, 100, 101, 99, 100, 1]]

    monkeypatch.setattr(ccxt_client, "CcxtPublicClient", FakeRest)
    frame = await _default_warmup(CFG, 12)
    assert len(frame) == 1
    _, _, kwargs = calls[0]
    assert set(kwargs) == {"since_ms", "until_ms"}
    assert kwargs["until_ms"] - kwargs["since_ms"] == 12 * HOUR


def test_default_account_requires_and_passes_credentials(monkeypatch):
    from vnedge.exchange import readonly_account

    monkeypatch.delenv("VNEDGE_EXEC_API_KEY", raising=False)
    monkeypatch.delenv("VNEDGE_EXEC_API_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="requires VNEDGE_EXEC_API_KEY"):
        _default_account(CFG)

    captured = {}

    class FakeProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def account_state(self):
            return None

    monkeypatch.setattr(readonly_account, "CcxtReadOnlyAccountProvider", FakeProvider)
    monkeypatch.setenv("VNEDGE_EXEC_API_KEY", "key")
    monkeypatch.setenv("VNEDGE_EXEC_API_SECRET", "secret")
    provider = _default_account(CFG)
    assert captured == {
        "exchange_id": "binanceusdm",
        "api_key": "key",
        "api_secret": "secret",
    }
    assert hasattr(provider, "account_state")


def test_delta_default_adapter_dispatches_to_native_adapter(monkeypatch):
    from vnedge.exchange import delta_contracts, delta_execution

    monkeypatch.setenv("VNEDGE_EXEC_API_KEY", "key")
    monkeypatch.setenv("VNEDGE_EXEC_API_SECRET", "secret")
    spec = delta_contracts.DeltaContractSpec("BTCUSD", product_id=42)
    monkeypatch.setattr(delta_contracts, "fetch_india_contract_spec", lambda symbol: spec)
    captured = {}

    class FakeDeltaAdapter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(delta_execution, "DeltaRestExecutionAdapter", FakeDeltaAdapter)
    cfg = LiveTraderRunConfig("delta_india", "BTC/USD:USD", "1h")
    adapter = _default_adapter(cfg)
    assert isinstance(adapter, FakeDeltaAdapter)
    assert captured["product_ids"] == {"BTC/USD:USD": 42}
    assert captured["contract_specs"] == {"BTC/USD:USD": spec}
    assert captured["dry_run"] is False and captured["live_confirmed"] is True
