"""Read-only canonical decision source for a future Delta live feed.

No exchange OHLC fallback, order adapter, or capital permission. The current
live trader continues to refuse Delta before clients are constructed. This
source proves that a scanner decision bar and its as-of 4h/1d parents can be
read from the same hashed lake as research.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import pandas as pd

from vnedge.data.candles import TF_SECONDS, CanonicalBarRecord, merge_candles
from vnedge.data.symbols import canonical_symbol
from vnedge.strategy.scanner_contracts import scanner_runtime_contract

_CHILD = {"5m": "1m", "15m": "5m", "1h": "15m", "4h": "1h", "1d": "4h"}


class _RecordStore(Protocol):
    def read_records(self, symbol: str, timeframe: str) -> list[CanonicalBarRecord]: ...


class CanonicalLiveProofError(ValueError):
    """Closed canonical identity or complete-child proof is missing."""


@dataclass(frozen=True, slots=True)
class CanonicalDecisionFrame:
    decision: CanonicalBarRecord
    context_4h: CanonicalBarRecord
    context_1d: CanonicalBarRecord

    @property
    def hashes(self) -> dict[str, str]:
        return {
            self.decision.timeframe: self.decision.content_sha256,
            "4h": self.context_4h.content_sha256,
            "1d": self.context_1d.content_sha256,
        }


class CanonicalLiveDecisionSource:
    """Idempotent lake-only lookup at one registered decision close."""

    def __init__(self, store: _RecordStore) -> None:
        self._store = store

    def _records(self, symbol: str, timeframe: str) -> list[CanonicalBarRecord]:
        rows = self._store.read_records(symbol, timeframe)
        previous: datetime | None = None
        for row in rows:
            if (
                row.exchange != "delta_india"
                or row.symbol != symbol
                or row.timeframe != timeframe
            ):
                raise CanonicalLiveProofError(
                    f"contradictory canonical {timeframe} partition identity for {symbol}"
                )
            if previous is not None and row.open_time <= previous:
                raise CanonicalLiveProofError(f"non-unique canonical {timeframe} series")
            previous = row.open_time
        return rows

    @staticmethod
    def _prove(row: CanonicalBarRecord) -> None:
        if (
            row.source != "canonical_tick_lake"
            or not row.identity_persisted
            or not row.candle.is_closed
            or row.data_quality != "ok"
            or not row.coverage_ok
            or len(row.content_sha256) != 64
        ):
            raise CanonicalLiveProofError(
                f"unproven canonical {row.timeframe} row at {row.open_time.isoformat()}"
            )

    def _complete_children(
        self, symbol: str, parent: CanonicalBarRecord,
        cache: dict[str, list[CanonicalBarRecord]],
    ) -> None:
        child_tf = _CHILD.get(parent.timeframe)
        if child_tf is None:
            return
        if child_tf not in cache:
            cache[child_tf] = self._records(symbol, child_tf)
        children = cache[child_tf]
        expected_count = TF_SECONDS[parent.timeframe] // TF_SECONDS[child_tf]
        by_open = {row.open_time: row for row in children}
        interval = timedelta(seconds=TF_SECONDS[child_tf])
        proved_children: list[CanonicalBarRecord] = []
        for index in range(expected_count):
            opened = parent.open_time + index * interval
            child = by_open.get(opened)
            if child is None or child.close_time != opened + interval:
                raise CanonicalLiveProofError(
                    f"incomplete {child_tf} children for {parent.timeframe} "
                    f"parent {parent.open_time.isoformat()}"
                )
            self._prove(child)
            self._complete_children(symbol, child, cache)
            proved_children.append(child)
        merged = merge_candles(
            symbol, parent.timeframe, [row.candle for row in proved_children]
        )
        for field in (
            "open", "high", "low", "close", "volume", "quote_volume",
            "trade_count", "taker_buy_volume", "vwap",
        ):
            if getattr(parent.candle, field) != getattr(merged, field):
                raise CanonicalLiveProofError(
                    f"{parent.timeframe} parent differs from complete {child_tf} children"
                )

    def bind(
        self, symbol: str, decision_timeframe: str, decision_close: datetime,
    ) -> CanonicalDecisionFrame:
        if decision_timeframe not in {"5m", "15m", "1h"}:
            raise CanonicalLiveProofError("unregistered canonical decision timeframe")
        if decision_close.tzinfo is None or decision_close.utcoffset() is None:
            raise CanonicalLiveProofError("decision close must be timezone-aware")
        symbol = canonical_symbol(symbol)
        close = decision_close.astimezone(UTC)
        cache: dict[str, list[CanonicalBarRecord]] = {}
        decisions = cache.setdefault(
            decision_timeframe, self._records(symbol, decision_timeframe)
        )
        exact = [row for row in decisions if row.close_time == close]
        if len(exact) != 1:
            raise CanonicalLiveProofError("exact canonical decision close missing")
        decision = exact[0]
        self._prove(decision)
        parents: list[CanonicalBarRecord] = []
        for timeframe in ("4h", "1d"):
            if timeframe not in cache:
                cache[timeframe] = self._records(symbol, timeframe)
            candidates = [
                row for row in cache[timeframe]
                if row.close_time <= close
            ]
            if not candidates:
                raise CanonicalLiveProofError(f"as-of {timeframe} context missing")
            parent = candidates[-1]
            self._prove(parent)
            parents.append(parent)
        for row in (decision, *parents):
            self._complete_children(symbol, row, cache)
        return CanonicalDecisionFrame(decision, parents[0], parents[1])

    def proven_frame(
        self,
        symbol: str,
        timeframe: str,
        *,
        through_close: datetime,
        limit: int,
    ) -> pd.DataFrame:
        """Return only proven, closed lake rows for scanner warmup/evaluation."""
        if limit < 1:
            raise CanonicalLiveProofError("canonical frame limit must be positive")
        symbol = canonical_symbol(symbol)
        close = through_close.astimezone(UTC)
        cache = {timeframe: self._records(symbol, timeframe)}
        selected = [row for row in cache[timeframe] if row.close_time <= close][-limit:]
        if not selected:
            raise CanonicalLiveProofError(f"canonical {timeframe} history missing")
        for row in selected:
            self._prove(row)
            self._complete_children(symbol, row, cache)
        return pd.DataFrame({
            "timestamp": pd.to_datetime([row.open_time for row in selected], utc=True),
            "open": [float(row.candle.open) for row in selected],
            "high": [float(row.candle.high) for row in selected],
            "low": [float(row.candle.low) for row in selected],
            "close": [float(row.candle.close) for row in selected],
            "volume": [float(row.candle.volume) for row in selected],
            "quote_volume": [float(row.candle.quote_volume) for row in selected],
            "trade_count": [row.candle.trade_count for row in selected],
            "taker_buy_volume": [
                float(row.candle.taker_buy_volume) for row in selected
            ],
            "vwap": [
                float(row.candle.vwap) if row.candle.vwap is not None else float("nan")
                for row in selected
            ],
            "data_quality": [row.data_quality for row in selected],
            "is_closed": True,
            "timeframe": timeframe,
            "symbol": symbol,
            "exchange": "delta_india",
            "candle_source": [row.source for row in selected],
            "content_sha256": [row.content_sha256 for row in selected],
            "coverage_ok": [row.coverage_ok for row in selected],
        }).reset_index(drop=True)


@dataclass(frozen=True, slots=True)
class RegisteredCanonicalScannerFrames:
    decision: pd.DataFrame
    contexts: dict[str, pd.DataFrame]
    proof: CanonicalDecisionFrame


class RegisteredCanonicalScannerSource:
    """Bind one registered scanner to lake-only decision and context frames."""

    def __init__(
        self,
        source: CanonicalLiveDecisionSource,
        *,
        strategy_id: str,
        symbol: str,
        exchange: str = "delta_india",
    ) -> None:
        contract = scanner_runtime_contract(strategy_id)
        if contract is None:
            raise CanonicalLiveProofError("strategy has no registered scanner contract")
        if exchange != "delta_india":
            raise CanonicalLiveProofError("canonical live source is Delta India only")
        symbol = canonical_symbol(symbol)
        if contract.allowed_symbols and symbol not in contract.allowed_symbols:
            raise CanonicalLiveProofError("symbol outside registered scanner contract")
        if contract.allowed_exchanges and exchange not in contract.allowed_exchanges:
            raise CanonicalLiveProofError("exchange outside registered scanner contract")
        self.source = source
        self.contract = contract
        self.strategy_id = strategy_id
        self.symbol = symbol

    def load(
        self,
        decision_close: datetime,
        *,
        decision_bars: int = 800,
        context_bars: int = 800,
    ) -> RegisteredCanonicalScannerFrames:
        proof = self.source.bind(
            self.symbol, self.contract.decision_tf, decision_close
        )
        decision = self.source.proven_frame(
            self.symbol, self.contract.decision_tf,
            through_close=decision_close, limit=decision_bars,
        )
        contexts = {
            timeframe: self.source.proven_frame(
                self.symbol, timeframe,
                through_close=decision_close, limit=context_bars,
            )
            for timeframe in self.contract.context_tfs
        }
        if decision.iloc[-1]["content_sha256"] != proof.decision.content_sha256:
            raise CanonicalLiveProofError("scanner frame/proof decision hash mismatch")
        return RegisteredCanonicalScannerFrames(decision, contexts, proof)

    def bind_strategy(
        self,
        strategy: object,
        decision_close: datetime,
        *,
        decision_bars: int = 800,
        context_bars: int = 800,
    ) -> RegisteredCanonicalScannerFrames:
        if getattr(strategy, "strategy_id", None) != self.strategy_id:
            raise CanonicalLiveProofError("strategy instance does not match registration")
        frames = self.load(
            decision_close,
            decision_bars=decision_bars,
            context_bars=context_bars,
        )
        binder = getattr(strategy, "bind_canonical_context", None)
        if frames.contexts and not callable(binder):
            raise CanonicalLiveProofError("registered context binder missing")
        for timeframe, frame in frames.contexts.items():
            binder(timeframe, frame.copy())
        return frames
