"""Research-only live Delta scalper scanner.

Public WebSocket/REST data in; append-only research decisions and an atomic
dashboard snapshot out.  This process deliberately constructs no account
client, execution adapter, OrderManager, or broker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from vnedge.data.delta_native_history import fetch_delta_candle_history
from vnedge.exchange.delta_ws import DeltaPublicWsClient
from vnedge.execution.journal import DecisionJournal
from vnedge.scalping.delta_engine.candle_store import (
    TIMEFRAME_SECONDS,
    MultiTimeframeCandleStore,
)
from vnedge.scalping.delta_engine.config import DeltaScalperConfig, load_delta_scalper_config
from vnedge.scalping.delta_engine.context import MarketContextBuilder
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.scanners import (
    ImbalanceFadeConfig,
    MomentumBurstConfig,
    MomentumBurstScanner,
    OrderFlowImbalanceFadeScanner,
)
from vnedge.scalping.delta_engine.signal_generator import (
    DeltaScalperSignalGenerator,
    SignalGateConfig,
)

DEFAULT_SYMBOLS = ("BTCUSD", "ETHUSD")
DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")


def _publish(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


class DeltaScalperShadowService:
    def __init__(
        self,
        symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
        *,
        snapshot_path: Path | str = "research/live_research/delta_scalper_engine_latest.json",
        journal_path: Path | str = "logs/delta_scalper/delta_scalper_shadow.journal.jsonl",
        deto_enabled: bool = False,
        scalper_opted_in: bool = False,
        config: DeltaScalperConfig | None = None,
    ) -> None:
        settings = config or DeltaScalperConfig()
        self.symbols = tuple(symbol.upper() for symbol in symbols)
        self.snapshot_path = Path(snapshot_path)
        self.store = MultiTimeframeCandleStore(max_bars_per_timeframe=700)
        self.context = MarketContextBuilder(self.store)
        self.fee_model = DeltaFeeModel(
            deto_enabled=deto_enabled or settings.fee_model.deto_enabled,
            scalper_opted_in=scalper_opted_in or settings.fee_model.scalper_opted_in,
            maker_fee_bps_pre_tax=settings.fee_model.maker_fee_bps_pre_tax,
            taker_fee_bps_pre_tax=settings.fee_model.taker_fee_bps_pre_tax,
            gst_rate=settings.fee_model.gst_rate,
            default_slippage_bps_per_leg=settings.fee_model.default_slippage_bps_per_leg,
        )
        self.journal = DecisionJournal(journal_path)
        self.generator = DeltaScalperSignalGenerator(
            self.context,
            (
                *(
                    (
                        MomentumBurstScanner(
                            self.fee_model,
                            config=MomentumBurstConfig(
                                min_volume_z=settings.scanners.momentum_burst.min_volume_z,
                                min_body_ratio=settings.scanners.momentum_burst.min_body_ratio,
                                min_breakout_bps=settings.scanners.momentum_burst.min_breakout_bps,
                                time_stop_seconds=(
                                    settings.scanners.momentum_burst.time_stop_seconds
                                ),
                                prefer_maker=settings.scanners.momentum_burst.prefer_maker,
                            ),
                        ),
                    )
                    if settings.scanners.momentum_burst.enabled
                    else ()
                ),
                *(
                    (
                        OrderFlowImbalanceFadeScanner(
                            self.fee_model,
                            config=ImbalanceFadeConfig(
                                min_wick_ratio=settings.scanners.imbalance_fade.min_wick_ratio,
                                min_stretch_bps=settings.scanners.imbalance_fade.min_stretch_bps,
                                time_stop_seconds=(
                                    settings.scanners.imbalance_fade.time_stop_seconds
                                ),
                                prefer_maker=settings.scanners.imbalance_fade.prefer_maker,
                            ),
                        ),
                    )
                    if settings.scanners.imbalance_fade.enabled
                    else ()
                ),
            ),
            journal=self.journal,
            gates=SignalGateConfig(
                min_expectancy_bps=settings.engine.min_expectancy_bps,
                min_probability=settings.engine.min_probability,
                min_confidence=settings.engine.min_confidence,
                allowed_symbols=settings.engine.symbols,
            ),
        )
        self.trade_flow: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=250))
        self.latest_decision: dict[str, dict] = {}
        self.evaluations: dict[str, int] = defaultdict(int)
        self.alerts: dict[str, int] = defaultdict(int)
        self.started_at = datetime.now(UTC)
        self.ws = DeltaPublicWsClient(
            list(self.symbols),
            candle_timeframes=DEFAULT_TIMEFRAMES,
            on_book=self._on_book,
            on_trade=self._on_trade,
            on_candle=self._on_candle,
        )

    async def seed(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        lookback_days = {"1m": 3, "5m": 5, "15m": 14, "1h": 60, "4h": 120}
        for symbol in self.symbols:
            for timeframe in DEFAULT_TIMEFRAMES:
                frame = await fetch_delta_candle_history(
                    symbol,
                    resolution=timeframe,
                    start_s=int((current - timedelta(days=lookback_days[timeframe])).timestamp()),
                    end_s=int(current.timestamp()),
                )
                step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
                for row in frame.itertuples(index=False):
                    from vnedge.scalping.delta_engine.types import Candle

                    closed = Candle(
                        ts=row.timestamp.to_pydatetime() + step,
                        open=float(row.open),
                        high=float(row.high),
                        low=float(row.low),
                        close=float(row.close),
                        volume=float(row.volume),
                        tf=timeframe,
                    )
                    self.store.append_closed(symbol, closed, observed_at=current)

    def _on_book(self, symbol: str, bids: list, asks: list, _raw: dict) -> None:
        def weighted(side: list) -> float:
            total = 0.0
            for index, level in enumerate(side[:5]):
                try:
                    total += float(level["size"]) / (index + 1)
                except (KeyError, TypeError, ValueError):
                    continue
            return total

        bid, ask = weighted(bids), weighted(asks)
        imbalance = (bid - ask) / (bid + ask) if bid + ask else 0.0
        self.context.update_l2_confirmation(
            symbol,
            imbalance=max(-1.0, min(1.0, imbalance)),
            cvd=sum(self.trade_flow[symbol]),
            observed_at=datetime.now(UTC),
        )

    def _on_trade(self, symbol: str, trade: dict) -> None:
        sign = 1.0 if trade.get("side") == "buy" else -1.0
        self.trade_flow[symbol].append(
            sign * float(trade.get("price") or 0) * float(trade.get("size") or 0)
        )

    def _on_candle(self, symbol: str, timeframe: str, row: list) -> None:
        now = datetime.now(UTC)
        try:
            close_ts = datetime.fromtimestamp(float(row[0]) / 1000.0, tz=UTC) + timedelta(
                seconds=TIMEFRAME_SECONDS[timeframe]
            )
        except (IndexError, KeyError, TypeError, ValueError):
            return
        latest = self.store.latest(symbol, timeframe)
        # REST seeding can overlap the first WS rollover. The seeded closed bar
        # is already authoritative for research state; never regress or
        # conflict on that hand-off.
        if latest is not None and close_ts <= latest.ts:
            return
        if not self.store.from_delta_row(symbol, timeframe, row, observed_at=now):
            return
        if timeframe not in {"1m", "5m"}:
            return
        decision = self.generator.on_candle_closed(symbol, timeframe, now=now)
        self.evaluations[symbol] += 1
        if decision.selected is not None:
            self.alerts[symbol] += 1
        self.latest_decision[symbol] = decision.to_dict()
        self.publish()

    def publish(self) -> None:
        now = datetime.now(UTC)
        rows = []
        for symbol in self.symbols:
            decision = self.latest_decision.get(symbol)
            selected = decision.get("selected") if decision else None
            rows.append(
                {
                    "strategy_id": "delta_scalper_engine_v1",
                    "exchange": "delta_india",
                    "symbol": symbol,
                    "timeframe": "1m/5m + 15m/1h/4h context",
                    "state": "FIRING" if selected else "WAITING",
                    "latest_eval_ts": decision.get("decision_ts") if decision else None,
                    "latest_bar_ts": (
                        self.store.latest(symbol, "1m").ts.isoformat()
                        if self.store.latest(symbol, "1m") else None
                    ),
                    "latest_eval": {
                        "side": selected.get("side") if selected else None,
                        "signal": selected,
                        "l2_confirmation": (
                            selected.get("metadata", {}).get("l2_confirmation")
                            if selected else None
                        ),
                        "research_only": True,
                        "can_trade": False,
                        "can_promote": False,
                    },
                    "why": (
                        "fee-adjusted closed-candle setup passed research gates"
                        if selected else "waiting for a setup that clears probability and fee gates"
                    ),
                    "evaluations": self.evaluations[symbol],
                    "alerts": self.alerts[symbol],
                    "can_trade": False,
                    "can_promote": False,
                }
            )
        _publish(
            self.snapshot_path,
            {
                "generated_at": now.isoformat(),
                "mode": "delta_scalper_research_shadow",
                "summary": {
                    "connected_symbols": len(self.symbols),
                    "firing": sum(row["state"] == "FIRING" for row in rows),
                    "waiting": sum(row["state"] == "WAITING" for row in rows),
                    "uptime_seconds": (now - self.started_at).total_seconds(),
                    "evaluations": sum(self.evaluations.values()),
                    "alerts": sum(self.alerts.values()),
                },
                "fee_model": {
                    "maker_bps_including_gst": self.fee_model.maker_bps,
                    "taker_bps_including_gst": self.fee_model.taker_bps,
                    "deto_enabled": self.fee_model.deto_enabled,
                    "scalper_opted_in": self.fee_model.scalper_opted_in,
                },
                "rows": rows,
                "policy": {
                    "research_only": True,
                    "order_route_present": False,
                    "l2_is_confirmation_only": True,
                    "can_trade": False,
                    "can_promote": False,
                },
                "can_trade": False,
                "can_promote": False,
            },
        )

    async def run(self) -> None:
        await self.seed()
        self.publish()
        await self.ws.start()
        try:
            while True:
                for symbol, rate in self.ws.funding_rate.items():
                    self.context.update_funding(symbol, rate, datetime.now(UTC))
                await asyncio.sleep(5)
        finally:
            await self.ws.stop()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/delta_scalper.yaml")
    parser.add_argument("--symbols")
    parser.add_argument(
        "--snapshot", default="research/live_research/delta_scalper_engine_latest.json"
    )
    parser.add_argument("--deto", action="store_true")
    parser.add_argument("--scalper-opted-in", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = load_delta_scalper_config(args.config)
    symbols = (
        tuple(value.strip().upper() for value in args.symbols.split(",") if value.strip())
        if args.symbols
        else config.engine.symbols
    )
    service = DeltaScalperShadowService(
        symbols,
        snapshot_path=args.snapshot,
        deto_enabled=args.deto,
        scalper_opted_in=args.scalper_opted_in,
        config=config,
    )
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
