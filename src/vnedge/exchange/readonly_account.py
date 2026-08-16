"""Read-only account provider — real equity/positions for shadow sizing.

For "real shadow trading": use real Binance MAINNET market data (public,
keyless — already how the trial runs) but size against your REAL account
equity. That is the only thing keys add to a shadow bot.

SAFETY — this class is deliberately crippled:
- It calls ONLY read endpoints: fetch_balance, fetch_positions. There is NO
  create_order / cancel_order method anywhere in it. It CANNOT trade.
- Pair it with read-only API keys (Binance: enable "Reading" only, NOT
  trading, NOT withdrawals). Then no order can reach the exchange even if a
  bug tried — the permission doesn't exist server-side.
- Shadow execution stays on the PaperBroker/SimulatedExchange, which also
  has no exchange-submit path. Two independent walls.

Never log secrets. Keys come from env, never code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from vnedge.execution.order_manager import FlattenTarget
from vnedge.risk.risk_manager import AccountState

logger = logging.getLogger(__name__)


class ReadOnlyExchangeClient(Protocol):
    urls: dict[str, Any]

    async def close(self) -> None: ...

    async def fetch_balance(self) -> dict: ...

    async def fetch_positions(self) -> list[dict]: ...


@dataclass(frozen=True)
class PositionRead:
    """Explicit venue-position result; an empty success means verified flat."""

    positions: tuple[FlattenTarget, ...] = ()
    exposure_by_symbol_usd: tuple[tuple[str, float], ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class CcxtReadOnlyAccountProvider:
    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        *,
        api_key: str,
        api_secret: str,
        base_currency: str = "USDT",
        client: ReadOnlyExchangeClient | None = None,  # injectable for tests
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("read-only account provider requires API credentials")
        self.exchange_id = exchange_id
        self.base_currency = base_currency
        self._peak_equity_usd = 0.0
        self._session_day: date | None = None
        self._day_start_equity_usd = 0.0
        if client is not None:
            self._ex = client
        else:  # pragma: no cover - network client
            import ccxt.async_support as ccxt_async

            from vnedge.data.ccxt_client import (
                _API_URL_OVERRIDES,
                resolve_ccxt_exchange_id,
            )

            # No sandbox: real mainnet account. Keys MUST be read-only.
            self._ex = getattr(ccxt_async, resolve_ccxt_exchange_id(exchange_id))(
                {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
            )
            overrides = _API_URL_OVERRIDES.get(exchange_id)
            if overrides:
                self._ex.urls["api"] = {**self._ex.urls.get("api", {}), **overrides}

    async def close(self) -> None:
        await self._ex.close()

    async def fetch_equity_usd(self) -> float:
        """Real account equity (total base-currency balance). Read-only."""
        balance = await self._ex.fetch_balance()
        total = balance.get("total", {})
        equity = total.get(self.base_currency)
        if equity is None:
            raise RuntimeError(
                f"no {self.base_currency} balance found — check the account/keys"
            )
        return float(equity)

    async def open_positions(self) -> PositionRead:
        """Read positions without conflating an API failure with flat."""
        try:
            raw = await self._ex.fetch_positions()
        except Exception as exc:  # noqa: BLE001 — some accounts/venues 400 when flat
            reason = f"{type(exc).__name__}: {exc}"
            logger.error("fetch_positions failed (venue state unknown): %s", reason)
            return PositionRead(error=reason)
        out: list[FlattenTarget] = []
        exposure: dict[str, float] = {}
        for p in raw:
            contracts = float(p.get("contracts") or 0.0)
            if contracts == 0:
                continue
            side = p.get("side")  # "long" | "short"
            symbol = str(p.get("symbol", ""))
            out.append(FlattenTarget(
                symbol=symbol,
                side=side if side in ("long", "short") else ("long" if contracts > 0 else "short"),
                quantity=abs(contracts),
            ))
            notional = abs(float(p.get("notional") or 0.0))
            if notional == 0.0:
                contract_size = abs(float(p.get("contractSize") or 1.0))
                mark = abs(float(p.get("markPrice") or p.get("entryPrice") or 0.0))
                notional = abs(contracts) * contract_size * mark
            if notional <= 0.0:
                reason = f"position exposure unavailable for {symbol or '<unknown symbol>'}"
                logger.error("%s", reason)
                return PositionRead(error=reason)
            exposure[symbol] = exposure.get(symbol, 0.0) + notional
        return PositionRead(
            positions=tuple(out),
            exposure_by_symbol_usd=tuple(sorted(exposure.items())),
        )

    async def account_state(self) -> AccountState:
        """Build a gateway snapshot from authenticated venue truth."""
        balance = await self._ex.fetch_balance()
        total = balance.get("total", {}) or {}
        equity_raw = total.get(self.base_currency)
        if equity_raw is None:
            raise RuntimeError(f"no {self.base_currency} balance found — check account/keys")
        equity = float(equity_raw)
        position_read = await self.open_positions()
        if not position_read.ok:
            raise RuntimeError(f"position read failed: {position_read.error}")

        today = datetime.now(UTC).date()
        if self._session_day != today:
            self._session_day = today
            self._day_start_equity_usd = equity
        self._peak_equity_usd = max(self._peak_equity_usd, equity)
        exposure = dict(position_read.exposure_by_symbol_usd)
        return AccountState(
            equity_usd=equity,
            daily_pnl_usd=equity - self._day_start_equity_usd,
            peak_equity_usd=max(self._peak_equity_usd, equity),
            open_positions=len(position_read.positions),
            exposure_by_symbol_usd=exposure,
            total_exposure_usd=sum(exposure.values()),
        )
