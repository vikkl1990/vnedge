"""MarketContext assembly from closed candles plus optional context feeds."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime

from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.regime import RegimeEngine, build_features
from vnedge.scalping.delta_engine.types import L2Confirmation, MarketContext


class MarketContextBuilder:
    def __init__(
        self,
        candle_store: MultiTimeframeCandleStore,
        regime_engine: RegimeEngine | None = None,
        *,
        max_l2_age_seconds: float = 2.0,
    ) -> None:
        if max_l2_age_seconds <= 0:
            raise ValueError("max_l2_age_seconds must be positive")
        self.candle_store = candle_store
        self.regime_engine = regime_engine or RegimeEngine()
        self.max_l2_age_seconds = max_l2_age_seconds
        self._funding: dict[str, deque[tuple[datetime, float]]] = defaultdict(
            lambda: deque(maxlen=24)
        )
        self._l2: dict[str, L2Confirmation] = {}

    def update_funding(self, symbol: str, rate: float, observed_at: datetime) -> None:
        ts = observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at
        rows = self._funding[symbol.upper()]
        value = float(rate)
        if not rows or rows[-1][1] != value:
            rows.append((ts.astimezone(UTC), value))

    def update_l2_confirmation(
        self,
        symbol: str,
        *,
        imbalance: float,
        cvd: float,
        observed_at: datetime,
        status: str = "fresh",
    ) -> None:
        self._l2[symbol.upper()] = L2Confirmation(
            imbalance=imbalance,
            cvd=cvd,
            status=status,
            observed_at=observed_at,
        )

    def build(self, symbol: str, *, now: datetime | None = None) -> MarketContext:
        native = symbol.upper()
        current = now or datetime.now(UTC)
        candles = self.candle_store.snapshot(native)
        latest = max((rows[-1].ts for rows in candles.values() if rows), default=None)
        if latest is None:
            raise RuntimeError(f"no closed candles for {native}")
        if latest > current:
            raise ValueError("latest closed candle is in the future")
        funding_rows = self._funding[native]
        funding_rate = funding_rows[-1][1] if funding_rows else 0.0
        funding_velocity = (
            funding_rows[-1][1] - funding_rows[-2][1] if len(funding_rows) >= 2 else 0.0
        )
        l2 = self._l2.get(native, L2Confirmation())
        if l2.observed_at is not None and (
            current.astimezone(UTC) - l2.observed_at
        ).total_seconds() > self.max_l2_age_seconds:
            l2 = L2Confirmation(
                imbalance=l2.imbalance,
                cvd=l2.cvd,
                status="stale",
                observed_at=l2.observed_at,
            )
        return MarketContext(
            symbol=native,
            ts=latest,
            candles=candles,
            regime=self.regime_engine.classify(candles, funding_rate=funding_rate),
            funding_rate=funding_rate,
            funding_velocity=funding_velocity,
            l2=l2,
            features=build_features(candles),
        )
