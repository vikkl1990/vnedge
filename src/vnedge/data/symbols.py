"""Canonical market identity shared by storage, routing, and evidence paths.

Exchange clients still receive their venue-native symbol (for example
``BTC/USDT:USDT``).  Everything that identifies persisted or replayable market
data uses :func:`canonical_symbol` instead.  Keeping that distinction explicit
prevents a lane from subscribing to ``BTC/USDT:USDT`` while the candle lake and
router publish ``BTCUSDT``.
"""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def canonical_symbol(symbol: str) -> str:
    """Return the stable data-plane symbol key.

    The settlement suffix after ``:`` is deliberately removed because the
    base/quote pair before it already distinguishes ``BTCUSDT`` from
    ``BTCUSD``.  Venue-native symbols must remain available separately for
    exchange API calls.
    """

    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")
    base = symbol.strip().upper().split(":", 1)[0]
    normalized = _NON_ALNUM.sub("", base)
    if not normalized:
        raise ValueError("symbol must contain at least one letter or digit")
    return normalized


__all__ = ["canonical_symbol"]
