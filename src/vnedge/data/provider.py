"""Market-data provider seam — the shape a non-CCXT source plugs into.

Today every source is CCXT (or the Delta native client). This is the interface
so a *future* non-CCXT source — a REST data vendor, an on-chain funding feed —
implements ONE protocol, declares a manifest, registers, and is usable, without
touching a line of the CCXT path. It is deliberately **additive**: existing
ingestion keeps calling ``CcxtPublicClient`` directly; a provider is opt-in.

Two guarantees:

- **deny-by-default** — only a registered provider is usable; an unknown name is
  refused, never silently constructed;
- **capability-gated** — a fetch for a capability the provider's manifest does
  not declare is refused *before* any network call.

The manifest is the "plugin manifest with permissions": it declares which
venues and capabilities a provider serves and whether it needs credentials.
Nothing here trades, sizes, or bypasses the data-quality gate — it only says
where candles / funding / open-interest come from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

CAP_CANDLES = "candles"
CAP_FUNDING = "funding"
CAP_OPEN_INTEREST = "open_interest"
CAPABILITIES: tuple[str, ...] = (CAP_CANDLES, CAP_FUNDING, CAP_OPEN_INTEREST)


@runtime_checkable
class MarketDataProvider(Protocol):
    """A source of historical market data for one venue.

    Method signatures mirror ``CcxtPublicClient`` exactly so the existing client
    is already a structural provider — the seam adds an interface, not a rewrite.
    Timestamps are epoch-ms; ranges are ``[since_ms, until_ms)``.
    """

    name: str

    async def fetch_candles(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int
    ) -> list[list]: ...

    async def fetch_funding_history(
        self, symbol: str, since_ms: int, until_ms: int
    ) -> list[dict]: ...

    async def fetch_open_interest_history(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int
    ) -> list[dict]: ...

    async def close(self) -> None: ...


class ProviderError(RuntimeError):
    """Raised for an unknown provider or an undeclared capability."""


@dataclass(frozen=True)
class ProviderManifest:
    """Declares what a provider serves — the plugin manifest with permissions."""

    name: str
    kind: str  # "ccxt" | "native" | "custom"
    venues: tuple[str, ...]
    capabilities: frozenset[str]
    requires_credentials: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        unknown = set(self.capabilities) - set(CAPABILITIES)
        if unknown:
            raise ValueError(
                f"provider {self.name!r} declares unknown capabilities {sorted(unknown)} "
                f"(known: {list(CAPABILITIES)})"
            )

    def declares(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "venues": list(self.venues),
            "capabilities": sorted(self.capabilities),
            "requires_credentials": self.requires_credentials,
            "notes": self.notes,
        }


@dataclass
class _Entry:
    manifest: ProviderManifest
    factory: Callable[[], MarketDataProvider]


class ProviderRegistry:
    """Deny-by-default registry of market-data providers."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def register(
        self, manifest: ProviderManifest, factory: Callable[[], MarketDataProvider]
    ) -> None:
        if manifest.name in self._entries:
            raise ProviderError(f"provider {manifest.name!r} already registered")
        self._entries[manifest.name] = _Entry(manifest, factory)

    def names(self) -> list[str]:
        return sorted(self._entries)

    def manifest(self, name: str) -> ProviderManifest:
        entry = self._entries.get(name)
        if entry is None:
            raise ProviderError(f"unknown provider {name!r} — register it first (deny-by-default)")
        return entry.manifest

    def manifests(self) -> list[dict]:
        return [self._entries[n].manifest.to_dict() for n in self.names()]

    def with_capability(self, capability: str) -> list[str]:
        return [n for n, e in sorted(self._entries.items()) if e.manifest.declares(capability)]

    def require_capability(self, name: str, capability: str) -> None:
        """Refuse a capability the provider's manifest doesn't declare — before
        any network call is made."""
        if not self.manifest(name).declares(capability):
            raise ProviderError(
                f"provider {name!r} does not declare capability {capability!r} "
                f"(declares: {sorted(self.manifest(name).capabilities)})"
            )

    def create(self, name: str) -> MarketDataProvider:
        """Construct a provider by name. Unknown name → refused (deny-by-default)."""
        entry = self._entries.get(name)
        if entry is None:
            raise ProviderError(f"unknown provider {name!r} — register it first (deny-by-default)")
        return entry.factory()
