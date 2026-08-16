"""Live-trader entrypoint — the ONLY runtime wiring that can run a
``LiveTraderSession`` against a real venue.

Its whole job is to be a fail-closed gate before any live client is ever built:

  1. the three live gates (``settings.is_live``: a live_* mode AND
     ``live_trading_enabled`` AND the exact confirmation phrase);
  2. the fail-closed pre-live checklist (kill switch clear, risk config frozen,
     mode ladder attested, reconciliation clean, journal writable, ...);
  3. mainnet trade-only credentials present.

If ANY of those is not satisfied it logs why and returns non-zero WITHOUT
constructing a single live client — wiring this entrypoint therefore does not
make accidental live trading any easier: the operator still has to open every
gate, install keys, and attest the ladder. Only when all pass does it wire the
proven live components (execution adapter, read-only account provider, live
feed, gateway, order manager, reconciler) and run the session.

Live dependency constructors are injected (``*_factory``) so the gate chain is
testable end-to-end without ever touching a real venue.

Run (only meaningful once the operator has opened the gates + installed keys):
  python -m vnedge.runtime.live_trader_main --exchange delta_india \
      --symbol BTC/USD:USD --timeframe 1h --strategy trend_continuation_v1
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from vnedge.config.settings import LIVE_CONFIRMATION_PHRASE, Settings
from vnedge.data.schemas import normalize_candles
from vnedge.exchange.venue_specs import venue_symbol_limits
from vnedge.execution.fill_ledger import FillLedger
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.live_reconciliation import LiveReconciler
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.private_stream import (
    PrivateStreamEventApplier,
    PrivateStreamHealth,
)
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.live_trader import LiveTraderSession
from vnedge.runtime.pre_live_checklist import run_pre_live_checklist_from_env

logger = logging.getLogger(__name__)

#: exit codes — non-zero means "refused, no live client built".
_EXIT_OK = 0
_EXIT_GATES = 10
_EXIT_CHECKLIST = 11
_EXIT_CREDENTIALS = 12
_EXIT_STRATEGY = 13

_WARMUP_BARS = 500


@dataclass(frozen=True)
class LiveTraderRunConfig:
    exchange: str
    symbol: str
    timeframe: str = "1h"
    strategy_id: str = "measurement_only_v1"
    # exit engine config — set these to the SAME values the strategy was judged
    # under so live exits match paper/shadow/backtest (A1). Defaults mirror
    # RunnerConfig; a trailed strategy (e.g. crypto_trend) must pass its trail.
    max_holding_bars: int = 48
    trail_atr_mult: float = 0.0
    trail_atr_window: int = 14


def _credentials_present() -> bool:
    return bool(os.environ.get("VNEDGE_EXEC_API_KEY")) and bool(
        os.environ.get("VNEDGE_EXEC_API_SECRET")
    )


async def _default_warmup(config: LiveTraderRunConfig, bars: int) -> pd.DataFrame:
    from vnedge.data.ccxt_client import CcxtPublicClient

    timeframe_ms = _timeframe_milliseconds(config.timeframe)
    until_ms = int(datetime.now(UTC).timestamp() * 1000)
    since_ms = until_ms - timeframe_ms * max(int(bars), 1)
    async with CcxtPublicClient(config.exchange) as rest:
        raw = await rest.fetch_candles(
            config.symbol,
            config.timeframe,
            since_ms=since_ms,
            until_ms=until_ms,
        )
    return normalize_candles(raw)


def _timeframe_milliseconds(timeframe: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    raw = str(timeframe).strip().lower()
    if len(raw) < 2 or raw[-1] not in units:
        raise ValueError(f"unsupported timeframe for live warmup: {timeframe!r}")
    try:
        count = int(raw[:-1])
    except ValueError as exc:
        raise ValueError(f"invalid timeframe for live warmup: {timeframe!r}") from exc
    if count <= 0:
        raise ValueError("timeframe must be positive")
    return count * units[raw[-1]]


def _default_adapter(config: LiveTraderRunConfig):
    # The mainnet execution client. Constructed ONLY after the gate chain passes.
    api_key = os.environ["VNEDGE_EXEC_API_KEY"]
    api_secret = os.environ["VNEDGE_EXEC_API_SECRET"]
    if config.exchange.lower() in {"delta", "delta_india", "deltaindia"}:
        from vnedge.exchange.delta_contracts import fetch_india_contract_spec
        from vnedge.exchange.delta_execution import DeltaRestExecutionAdapter

        spec = fetch_india_contract_spec(config.symbol)
        if spec.product_id is None:
            raise RuntimeError(f"Delta product {config.symbol} has no product id")
        return DeltaRestExecutionAdapter(
            api_key=api_key,
            api_secret=api_secret,
            dry_run=False,
            live_confirmed=True,
            product_ids={config.symbol: spec.product_id},
            contract_specs={config.symbol: spec},
        )

    from vnedge.exchange.live_execution import CcxtExecutionAdapter

    return CcxtExecutionAdapter(
        config.exchange,
        api_key=api_key,
        api_secret=api_secret,
        testnet=False,
        live_confirmed=True,
    )


def _default_account(config: LiveTraderRunConfig):
    from vnedge.exchange.readonly_account import CcxtReadOnlyAccountProvider

    try:
        api_key = os.environ["VNEDGE_EXEC_API_KEY"]
        api_secret = os.environ["VNEDGE_EXEC_API_SECRET"]
    except KeyError as exc:
        raise RuntimeError(
            "live account provider requires VNEDGE_EXEC_API_KEY and "
            "VNEDGE_EXEC_API_SECRET"
        ) from exc
    return CcxtReadOnlyAccountProvider(
        exchange_id=config.exchange,
        api_key=api_key,
        api_secret=api_secret,
    )


def _default_feed(config: LiveTraderRunConfig):
    from vnedge.exchange.live_feed import LiveMarketFeed

    return LiveMarketFeed(
        config.exchange, symbol=config.symbol, timeframe=config.timeframe
    )


def _default_strategy(strategy_id: str):
    from vnedge.strategy.strategy_registry import get_strategy_class, is_capital_eligible

    if not is_capital_eligible(strategy_id):
        raise RuntimeError(f"strategy {strategy_id!r} is not capital eligible")
    return get_strategy_class(strategy_id)()


def _default_fill_ledger(config: LiveTraderRunConfig) -> FillLedger:
    return FillLedger(f"logs/live/{config.exchange}_{config.strategy_id}.fills.jsonl")


def _default_private_stream(config, applier, health):
    # The mainnet private order/fill stream. Constructed ONLY after the gate chain
    # passes; same live_confirmed gate as the execution adapter.
    if config.exchange.lower() in {"delta", "delta_india", "deltaindia"}:
        raise RuntimeError(
            "Delta live trading remains disabled: native private order/fill stream "
            "is not implemented. Execution is native, but live cannot start "
            "without private venue truth."
        )

    from vnedge.execution.private_stream import CcxtPrivateStream

    return CcxtPrivateStream(
        config.exchange,
        api_key=os.environ["VNEDGE_EXEC_API_KEY"],
        api_secret=os.environ["VNEDGE_EXEC_API_SECRET"],
        applier=applier,
        testnet=False,
        live_confirmed=True,
        health=health,
    )


async def run_live_trader(
    settings: Settings,
    config: LiveTraderRunConfig,
    *,
    adapter_factory=None,
    account_factory=None,
    feed_factory=None,
    strategy_factory=None,
    warmup_loader=None,
    fill_ledger_factory=None,
    private_stream_factory=None,
    max_bars: int | None = None,
) -> int:
    """Enforce the full gate chain, then (only if it clears) wire + run the
    live session. Returns 0 on a clean run, a non-zero code if refused."""
    # --- Gate 1: three live gates -------------------------------------------------
    if not settings.is_live:
        logger.error(
            "REFUSED: three live gates not open (mode=%s, enabled=%s, phrase_ok=%s). "
            "No live client constructed.",
            settings.trading_mode.value, settings.live_trading_enabled,
            settings.confirm_live_trading == LIVE_CONFIRMATION_PHRASE,
        )
        return _EXIT_GATES

    # --- Gate 2: fail-closed pre-live checklist -----------------------------------
    checklist = run_pre_live_checklist_from_env(settings)
    if not checklist.cleared:
        logger.error(
            "REFUSED: pre-live checklist not cleared (%s). No live client constructed.",
            ", ".join(f.name for f in checklist.failures),
        )
        return _EXIT_CHECKLIST

    # --- Gate 3: strategy registration and kill policy ---------------------------
    from vnedge.strategy.strategy_registry import is_capital_eligible

    if not is_capital_eligible(config.strategy_id):
        logger.error(
            "REFUSED: strategy %r is unknown, measurement-only, killed, or not "
            "explicitly capital-approved. "
            "No live client constructed.",
            config.strategy_id,
        )
        return _EXIT_STRATEGY

    # --- Gate 4: mainnet trade-only credentials -----------------------------------
    if not _credentials_present():
        logger.error(
            "REFUSED: VNEDGE_EXEC_API_KEY/SECRET not set. No live client constructed."
        )
        return _EXIT_CREDENTIALS

    # All gates open — NOW it is safe to build live clients.
    logger.warning(
        "ALL GATES OPEN — starting LIVE trader on %s %s (%s). This places REAL orders.",
        config.exchange, config.symbol, config.strategy_id,
    )
    adapter = (adapter_factory or _default_adapter)(config)
    account = (account_factory or _default_account)(config)
    feed = (feed_factory or _default_feed)(config)
    strategy = (strategy_factory or _default_strategy)(config.strategy_id)
    history = await (warmup_loader or _default_warmup)(config, _WARMUP_BARS)

    journal = DecisionJournal(f"logs/live/{config.exchange}_{config.strategy_id}.journal.jsonl")
    # H1: the field is a Path — a str here makes is_active()/.exists() crash on the
    # first entry, so `touch KILL` could not halt a live bot. Honor KILL_FILE so the
    # gateway's switch and the pre-live checklist (which reads KILL_FILE) agree.
    gateway = PreTradeRiskGateway(
        settings.risk, KillSwitch(kill_file=Path(os.environ.get("KILL_FILE", "KILL")))
    )
    om = OrderManager(gateway, journal, adapter)
    reconciler = LiveReconciler(om, adapter)
    limits = venue_symbol_limits(config.exchange, config.symbol)

    # M2: immutable fill ledger + the private order/fill stream. The stream is the
    # ONLY source of real per-fill economics (price/fee) AND the freshness signal
    # the session gates entries on — so require_private_stream is True on live.
    fill_ledger = (fill_ledger_factory or _default_fill_ledger)(config)
    health = PrivateStreamHealth()
    applier = PrivateStreamEventApplier(om, fill_ledger=fill_ledger, venue=config.exchange)
    stream = (private_stream_factory or _default_private_stream)(config, applier, health)

    session = LiveTraderSession(
        strategy, feed, history, settings=settings, gateway=gateway,
        order_manager=om, reconciler=reconciler, account_provider=account,
        symbol=config.symbol, limits=limits, pre_live_report=checklist,
        max_holding_bars=config.max_holding_bars,
        trail_atr_mult=config.trail_atr_mult,
        trail_atr_window=config.trail_atr_window,
        fill_ledger=fill_ledger,
        private_stream_health=health, require_private_stream=True,
    )
    stop_event = asyncio.Event()
    stream_task = asyncio.create_task(
        stream.run_forever(symbol=config.symbol, stop_event=stop_event)
    )
    try:
        await feed.start()
        await session.run(max_bars=max_bars)
    finally:
        stop_event.set()
        stream_task.cancel()
        try:
            await stream_task
        except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
            logger.debug("private stream teardown completed with error: %s", exc)
        for name, closer in (("feed", feed.stop), ("adapter", adapter.close),
                             ("stream", getattr(stream, "close", None))):
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s close failed: %s", name, exc)
    return _EXIT_OK


def _parse_args(argv=None) -> LiveTraderRunConfig:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exchange", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--strategy", dest="strategy_id", required=True)
    a = ap.parse_args(argv)
    return LiveTraderRunConfig(
        exchange=a.exchange, symbol=a.symbol, timeframe=a.timeframe,
        strategy_id=a.strategy_id,
    )


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    config = _parse_args(argv)
    return asyncio.run(run_live_trader(settings, config))


if __name__ == "__main__":
    raise SystemExit(main())
