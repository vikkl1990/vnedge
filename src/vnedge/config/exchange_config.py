"""Per-exchange configuration.

API credentials are read from dedicated environment variables (never from
code, never from committed files). Keys must be TRADE-ONLY: withdrawal
permission disabled and IP whitelisting enabled on the exchange side — this
module cannot verify that, so it is part of the pre-live checklist.
"""

from __future__ import annotations

import os
from enum import Enum

from pydantic import BaseModel, Field


class ExchangeId(str, Enum):
    BINANCE_FUTURES = "binance_futures"
    BYBIT = "bybit"
    DELTA_INDIA = "delta_india"


class ExchangeCredentials(BaseModel):
    """API credentials. Repr is redacted so secrets never reach logs."""

    api_key: str = ""
    api_secret: str = ""

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "ExchangeCredentials(api_key=***, api_secret=***)"

    __str__ = __repr__

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)


class ExchangeConfig(BaseModel):
    exchange_id: ExchangeId
    testnet: bool = True  # safe default: real venues are opt-in
    credentials: ExchangeCredentials = Field(default_factory=ExchangeCredentials)
    # Symbols this venue is allowed to trade; empty means "not yet approved".
    allowed_symbols: list[str] = Field(default_factory=list)


_ENV_PREFIX = {
    ExchangeId.BINANCE_FUTURES: "BINANCE",
    ExchangeId.BYBIT: "BYBIT",
    ExchangeId.DELTA_INDIA: "DELTA",
}


def load_exchange_config(exchange_id: ExchangeId) -> ExchangeConfig:
    """Build config for one venue from environment variables."""
    prefix = _ENV_PREFIX[exchange_id]
    return ExchangeConfig(
        exchange_id=exchange_id,
        testnet=os.environ.get(f"{prefix}_TESTNET", "true").lower() != "false",
        credentials=ExchangeCredentials(
            api_key=os.environ.get(f"{prefix}_API_KEY", ""),
            api_secret=os.environ.get(f"{prefix}_API_SECRET", ""),
        ),
    )
