"""Read-only account provider — reads real equity/positions, cannot trade."""

import pytest

from vnedge.exchange.readonly_account import CcxtReadOnlyAccountProvider


class FakeCcxt:
    def __init__(self, balance=None, positions=None, positions_raises=False):
        self._balance = balance or {"total": {"USDT": 1234.56}}
        self._positions = positions or []
        self._positions_raises = positions_raises

    async def fetch_balance(self):
        return self._balance

    async def fetch_positions(self):
        if self._positions_raises:
            raise RuntimeError("400 no positions")
        return self._positions

    async def close(self):
        pass


def provider(**kw):
    return CcxtReadOnlyAccountProvider(api_key="k", api_secret="s",
                                       client=FakeCcxt(**kw))


def test_requires_credentials():
    with pytest.raises(ValueError, match="credentials"):
        CcxtReadOnlyAccountProvider(api_key="", api_secret="", client=FakeCcxt())


async def test_fetch_real_equity():
    p = provider(balance={"total": {"USDT": 987.65}})
    assert await p.fetch_equity_usd() == pytest.approx(987.65)


async def test_missing_balance_raises():
    p = provider(balance={"total": {}})
    with pytest.raises(RuntimeError, match="no USDT balance"):
        await p.fetch_equity_usd()


async def test_open_positions_mapped():
    p = provider(positions=[
        {"symbol": "BTC/USDT:USDT", "contracts": 0.01, "side": "long",
         "markPrice": 50_000.0},
        {"symbol": "ETH/USDT:USDT", "contracts": 0.0, "side": "long"},   # flat -> skipped
        {"symbol": "SOL/USDT:USDT", "contracts": 2.0, "side": "short",
         "notional": 240.0},
    ])
    result = await p.open_positions()
    assert result.ok
    assert len(result.positions) == 2
    assert result.positions[0].symbol == "BTC/USDT:USDT" and result.positions[0].side == "long"
    assert result.positions[1].side == "short" and result.positions[1].quantity == 2.0
    assert dict(result.exposure_by_symbol_usd)["BTC/USDT:USDT"] == 500.0


async def test_position_error_is_not_reported_as_flat():
    p = provider(positions_raises=True)
    result = await p.open_positions()
    assert not result.ok
    assert result.positions == ()
    assert "400 no positions" in str(result.error)


async def test_unknown_position_exposure_fails_closed():
    p = provider(positions=[
        {"symbol": "BTC/USDT:USDT", "contracts": 0.01, "side": "long"},
    ])
    result = await p.open_positions()
    assert not result.ok
    assert "exposure unavailable" in str(result.error)


def test_has_no_order_submission_methods():
    """The hard wall: a read-only provider must expose NO way to trade."""
    p = provider()
    for forbidden in ("create_order", "submit_order", "cancel_order",
                      "place_order", "submit"):
        assert not hasattr(p, forbidden), f"read-only provider must not have {forbidden}"
