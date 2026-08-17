"""Time Machine — multi-TF candle awareness (forming + closed), strictly causal.

Maintains a live, consistent view of several timeframes per symbol, including
the currently *forming* (not-yet-closed) bar. Forming bars are for awareness and
timing only — the invariant that **final decisions use only closed bars of the
decision timeframe** is untouched: nothing here places an order or emits an
intent. Never invents future prices; timestamps are exchange-referenced UTC.

Phase T1: in-memory store, kline + trade-aggregation ingestion, forming/closed
transitions, gap/stall/future guards, throttled forming events, and a read-only
snapshot. Deterministic and wall-clock-free (callers pass timestamps), so it is
fully unit-testable.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

TF = Literal["1m", "5m", "15m", "1h", "4h"]

_TF_SECONDS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

Health = Literal["ok", "stale", "gapped", "future"]


@dataclass(frozen=True)
class CandleState:
    symbol: str
    tf: TF
    open_time: datetime          # bar open (exchange time, UTC)
    close_time: datetime         # expected bar close
    is_closed: bool
    open: float
    high: float
    low: float
    close: float
    volume: float
    forming_progress: float      # 0.0 → 1.0 (meaningful only while forming)
    last_update_ts: datetime     # last processed update time
    exchange_ts: datetime        # last exchange timestamp used
    sequence_id: int             # monotonic per symbol+tf
    source: str                  # "ws_kline" | "trade_agg" | "rest_backfill"


@dataclass
class TimeMachineSnapshot:
    symbol: str
    as_of: datetime
    forming: dict[TF, CandleState]
    last_closed: dict[TF, CandleState]
    health: dict[TF, Health]


@dataclass(frozen=True)
class TimeMachineConfig:
    tfs: tuple[TF, ...] = ("1m", "5m", "15m", "1h", "4h")
    forming_update_throttle_ms: int = 500
    gap_threshold_mult: float = 1.5
    stall_threshold_mult: float = 2.5
    future_tolerance_ms: int = 2000
    max_history_closed: int = 500
    enable_trade_aggregation: bool = True


def _floor_open(ts: datetime, dur_s: int) -> datetime:
    """Floor a timestamp down to its timeframe boundary (UTC epoch aligned)."""
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % dur_s), tz=ts.tzinfo)


class TimeMachine:
    """Multi-TF candle state with forming-bar awareness. Never bypasses risk."""

    def __init__(
        self,
        symbols: list[str],
        tfs: list[TF] | None = None,
        config: TimeMachineConfig | None = None,
    ) -> None:
        self.config = config or TimeMachineConfig()
        self.tfs: tuple[TF, ...] = tuple(tfs) if tfs is not None else self.config.tfs
        self.symbols = list(symbols)
        self._forming: dict[tuple[str, TF], CandleState] = {}
        self._closed: dict[tuple[str, TF], deque[CandleState]] = defaultdict(
            lambda: deque(maxlen=self.config.max_history_closed)
        )
        self._seq: dict[tuple[str, TF], int] = defaultdict(int)
        self._health: dict[tuple[str, TF], Health] = {}
        self._last_update: dict[tuple[str, TF], datetime] = {}
        self._last_forming_emit: dict[tuple[str, TF], datetime] = {}
        self._callbacks: dict[str, list[Callable]] = defaultdict(list)

    # --- events ------------------------------------------------------------------
    def subscribe(self, event: str, callback: Callable) -> None:
        """event ∈ {'closed_bar', 'forming_update', 'gap', 'stall'}."""
        self._callbacks[event].append(callback)

    def _emit(self, event: str, *args) -> None:
        for cb in self._callbacks.get(event, ()):
            cb(*args)

    # --- ingestion ---------------------------------------------------------------
    def on_kline_update(
        self, symbol: str, tf: TF, kline: dict, is_closed: bool
    ) -> None:
        """Apply an exchange kline (full running OHLCV) for `tf`.

        `kline` keys: open_time (datetime), open, high, low, close, volume,
        exchange_ts (datetime, defaults to open_time).
        """
        open_time = kline["open_time"]
        ex_ts = kline.get("exchange_ts", open_time)
        self._apply_bar(
            symbol, tf, open_time,
            float(kline["open"]), float(kline["high"]), float(kline["low"]),
            float(kline["close"]), float(kline["volume"]),
            ex_ts, is_closed, source="ws_kline",
        )

    def on_trade(
        self, symbol: str, price: float, size: float, exchange_ts: datetime
    ) -> None:
        """Aggregate a trade into the forming 1m bar (DIY candle from the tape)."""
        if not self.config.enable_trade_aggregation or "1m" not in self.tfs:
            return
        dur = _TF_SECONDS["1m"]
        open_time = _floor_open(exchange_ts, dur)
        forming = self._forming.get((symbol, "1m"))
        if forming is not None and forming.open_time == open_time:
            o, h, l = forming.open, max(forming.high, price), min(forming.low, price)
            v = forming.volume + size
        else:
            o = h = l = price
            v = size
        self._apply_bar(symbol, "1m", open_time, o, h, l, price, v,
                        exchange_ts, is_closed=False, source="trade_agg")

    def on_rest_backfill(self, symbol: str, tf: TF, candles: list[dict]) -> None:
        """Backfill CLOSED history (e.g. after a gap). Each dict as on_kline_update."""
        for k in candles:
            self.on_kline_update(symbol, tf, k, is_closed=True)

    # --- the causal core ---------------------------------------------------------
    def _apply_bar(
        self, symbol: str, tf: TF, open_time: datetime,
        o: float, h: float, l: float, c: float, v: float,
        exchange_ts: datetime, is_closed: bool, source: str,
    ) -> None:
        key = (symbol, tf)
        dur = _TF_SECONDS[tf]

        # (5) future-bar guard: reject a bar that opens materially in the future
        tol = timedelta(milliseconds=self.config.future_tolerance_ms)
        if open_time > exchange_ts + tol:
            self._health[key] = "future"
            return

        forming = self._forming.get(key)
        last_closed = self._closed[key][-1] if self._closed[key] else None

        # (1) monotonicity: drop bars that move backwards
        if forming is not None and open_time < forming.open_time:
            return
        if last_closed is not None and open_time < last_closed.open_time:
            return

        # (4) gap detection vs the most recent known bar
        ref = forming or last_closed
        if ref is not None and open_time > ref.open_time + timedelta(seconds=dur * self.config.gap_threshold_mult):
            self._health[key] = "gapped"
            missing = int((open_time - ref.open_time).total_seconds() // dur) - 1
            self._emit("gap", symbol, tf, {"missing": missing, "from": ref.open_time, "to": open_time})
        else:
            self._health[key] = "ok"

        # (2/3) forming → closed rollover: a newer open_time finalizes the prior bar
        if forming is not None and open_time > forming.open_time and not forming.is_closed:
            self._finalize(key, forming)

        self._seq[key] += 1
        progress = 0.0 if is_closed else min(
            max((exchange_ts - open_time).total_seconds() / dur, 0.0), 0.999
        )
        cs = CandleState(
            symbol=symbol, tf=tf, open_time=open_time,
            close_time=open_time + timedelta(seconds=dur), is_closed=is_closed,
            open=o, high=h, low=l, close=c, volume=v, forming_progress=progress,
            last_update_ts=exchange_ts, exchange_ts=exchange_ts,
            sequence_id=self._seq[key], source=source,
        )
        self._last_update[key] = exchange_ts

        if is_closed:
            self._closed[key].append(cs)
            self._forming.pop(key, None)
            self._emit("closed_bar", symbol, tf, cs)
        else:
            self._forming[key] = cs
            self._maybe_emit_forming(key, cs, exchange_ts)

    def _finalize(self, key: tuple[str, TF], forming: CandleState) -> None:
        closed = replace(forming, is_closed=True, forming_progress=1.0)
        self._closed[key].append(closed)
        self._forming.pop(key, None)
        self._emit("closed_bar", forming.symbol, forming.tf, closed)

    def _maybe_emit_forming(
        self, key: tuple[str, TF], cs: CandleState, exchange_ts: datetime
    ) -> None:
        last = self._last_forming_emit.get(key)
        throttle = timedelta(milliseconds=self.config.forming_update_throttle_ms)
        if last is None or exchange_ts - last >= throttle:
            self._last_forming_emit[key] = exchange_ts
            self._emit("forming_update", cs.symbol, cs.tf, cs)

    # --- health check (caller drives the clock; no wall-clock dependency) ---------
    def check_health(self, now: datetime) -> None:
        """Mark stale TFs (no update in > stall_threshold_mult × tf) + emit stall."""
        for (symbol, tf), last in list(self._last_update.items()):
            dur = _TF_SECONDS[tf]
            if (
                now - last > timedelta(seconds=dur * self.config.stall_threshold_mult)
                and self._health.get((symbol, tf)) != "stale"
            ):
                self._health[(symbol, tf)] = "stale"
                self._emit("stall", symbol, tf)

    # --- queries -----------------------------------------------------------------
    def health_of(self, symbol: str, tf: TF) -> Health:
        """Current health of one (symbol, tf). Unknown/never-updated reads ``ok``
        so a TF the lane does not drive cannot spuriously block its arm-gate."""
        return self._health.get((symbol, tf), "ok")

    def age_ms(self, symbol: str, tf: TF, now: datetime) -> float | None:
        """Milliseconds since the last exchange update for (symbol, tf), or None
        if the TF has never updated. Used by the decision-path latency budget."""
        last = self._last_update.get((symbol, tf))
        if last is None:
            return None
        return max(0.0, (now - last).total_seconds() * 1000.0)

    def get_forming(self, symbol: str, tf: TF) -> CandleState | None:
        return self._forming.get((symbol, tf))

    def get_last_closed(self, symbol: str, tf: TF) -> CandleState | None:
        dq = self._closed.get((symbol, tf))
        return dq[-1] if dq else None

    def get_closed_history(self, symbol: str, tf: TF, n: int) -> list[CandleState]:
        dq = self._closed.get((symbol, tf))
        return list(dq)[-n:] if dq else []

    def get_state(self, symbol: str) -> TimeMachineSnapshot:
        forming, last_closed, health = {}, {}, {}
        as_of: datetime | None = None
        for tf in self.tfs:
            f = self._forming.get((symbol, tf))
            lc = self.get_last_closed(symbol, tf)
            if f is not None:
                forming[tf] = f
            if lc is not None:
                last_closed[tf] = lc
            health[tf] = self._health.get((symbol, tf), "ok")
            ref = f or lc
            if ref is not None and (as_of is None or ref.exchange_ts > as_of):
                as_of = ref.exchange_ts
        return TimeMachineSnapshot(
            symbol=symbol, as_of=as_of or datetime(1970, 1, 1, tzinfo=UTC),
            forming=forming, last_closed=last_closed, health=health,
        )

    def snapshot_dict(self, symbol: str, now: datetime | None = None) -> dict:
        """Compact read-only dict for the state snapshot / dashboard.

        ``now`` (optional) adds a per-TF ``age_ms`` block (now - last exchange
        update) so the health cockpit can colour forming-state freshness against
        the shared latency budgets.
        """
        st = self.get_state(symbol)
        out = {
            "as_of": st.as_of.isoformat() if st.as_of.tzinfo else None,
            "forming": {
                tf: {
                    "open_time": c.open_time.isoformat(),
                    "close_time": c.close_time.isoformat(),
                    "progress": round(c.forming_progress, 3),
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for tf, c in st.forming.items()
            },
            "last_closed": {
                tf: {"open_time": c.open_time.isoformat(), "close": c.close}
                for tf, c in st.last_closed.items()
            },
            "health": dict(st.health),
        }
        if now is not None:
            out["age_ms"] = {
                tf: round(a, 1)
                for tf in self.tfs
                if (a := self.age_ms(symbol, tf, now)) is not None
            }
        return out
