"""Runnable read-only Delta private-truth observer.

This process has no OrderManager, execution adapter, capital allowlist, or
live-trading settings. It records private evidence and remains unhealthy until
each socket generation is reconciled against complete REST snapshots/fills.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from vnedge.data.symbols import canonical_symbol
from vnedge.exchange.delta_private_truth import (
    DeltaPrivateFillEvidenceStore,
    DeltaPrivateRestBaselineReader,
    DeltaPrivateTruthObserver,
)
from vnedge.exchange.delta_private_ws import DeltaPrivateWsClient

logger = logging.getLogger(__name__)
_INDIA_BASE = "https://api.india.delta.exchange"


@dataclass(frozen=True, slots=True)
class DeltaPrivateObserverConfig:
    symbols: tuple[str, ...]
    product_ids: Mapping[str, int]
    ledger_path: Path
    reconcile_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        normalized = tuple(dict.fromkeys(canonical_symbol(s) for s in self.symbols))
        products = {canonical_symbol(s): int(pid) for s, pid in self.product_ids.items()}
        if not normalized or set(normalized) != set(products):
            raise ValueError("observer symbols must exactly match frozen product IDs")
        if any(pid <= 0 for pid in products.values()):
            raise ValueError("observer product IDs must be positive")
        if self.reconcile_interval_seconds <= 0:
            raise ValueError("observer reconcile interval must be positive")
        object.__setattr__(self, "symbols", normalized)
        object.__setattr__(self, "product_ids", products)


def parse_product_ids(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        symbol, separator, raw_id = item.strip().partition(":")
        if not separator or not symbol or not raw_id:
            raise ValueError("product IDs must use SYMBOL:ID comma-separated format")
        canonical = canonical_symbol(symbol)
        if canonical in result:
            raise ValueError(f"duplicate observer symbol {canonical}")
        result[canonical] = int(raw_id)
    if not result:
        raise ValueError("at least one observer product ID is required")
    return result


async def run_observer(
    config: DeltaPrivateObserverConfig,
    *,
    api_key: str,
    api_secret: str,
    stop_event: asyncio.Event | None = None,
    rest_client_factory=None,
    socket_factory=None,
) -> DeltaPrivateTruthObserver:
    if not api_key or not api_secret:
        raise ValueError("read-only Delta observer credentials are required")
    evidence = DeltaPrivateFillEvidenceStore(
        config.ledger_path, symbols=config.symbols
    )
    socket = (socket_factory or DeltaPrivateWsClient)(
        config.symbols,
        api_key=api_key,
        api_secret=api_secret,
        on_event=evidence.record_event,
    )
    if rest_client_factory is None:
        from delta_rest_client import DeltaRestClient

        rest_client_factory = DeltaRestClient
    rest = rest_client_factory(
        base_url=_INDIA_BASE, api_key=api_key, api_secret=api_secret
    )
    observer = DeltaPrivateTruthObserver(
        socket,
        evidence,
        DeltaPrivateRestBaselineReader(rest, product_ids=config.product_ids),
        reconcile_interval_seconds=config.reconcile_interval_seconds,
    )
    await observer.run_forever(stop_event=stop_event or asyncio.Event())
    return observer


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delta private evidence observer")
    parser.add_argument(
        "--products",
        default=os.environ.get("VNEDGE_DELTA_OBSERVER_PRODUCTS", ""),
        help="Exact Delta products as SYMBOL:PRODUCT_ID,...",
    )
    parser.add_argument(
        "--ledger",
        default=os.environ.get(
            "VNEDGE_DELTA_PRIVATE_LEDGER", "data/private/delta/fills.jsonl"
        ),
    )
    parser.add_argument(
        "--reconcile-seconds",
        type=float,
        default=float(os.environ.get("VNEDGE_DELTA_RECONCILE_SECONDS", "5")),
    )
    return parser.parse_args()


async def _main_async(args: argparse.Namespace) -> None:
    products = parse_product_ids(args.products)
    config = DeltaPrivateObserverConfig(
        symbols=tuple(products),
        product_ids=products,
        ledger_path=Path(args.ledger),
        reconcile_interval_seconds=args.reconcile_seconds,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await run_observer(
        config,
        api_key=os.environ.get("VNEDGE_DELTA_READ_API_KEY", ""),
        api_secret=os.environ.get("VNEDGE_DELTA_READ_API_SECRET", ""),
        stop_event=stop,
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(_main_async(_arguments()))


if __name__ == "__main__":
    main()
