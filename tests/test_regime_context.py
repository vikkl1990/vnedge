from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.data.candles import Candle
from vnedge.data.regime_context import (
    REGIME_CONFIG,
    RegimeLabel,
    detect_regime,
)


def _bars(*, count: int | None = None, timeframe: str = "1h") -> list[Candle]:
    total = count or REGIME_CONFIG.warmup_bars + 10
    step = timedelta(hours=4 if timeframe == "4h" else 1)
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    result: list[Candle] = []
    for index in range(total):
        close = Decimal(100 + index) + Decimal(index % 3) / Decimal(10)
        result.append(
            Candle(
                symbol="BTCUSDT",
                timeframe=timeframe,
                open_time=opened + index * step,
                close_time=opened + (index + 1) * step,
                open=close - Decimal("0.2"),
                high=close + Decimal("0.7"),
                low=close - Decimal("0.8"),
                close=close,
                volume=Decimal(100),
                quote_volume=close * Decimal(100),
                trade_count=20,
            )
        )
    return result


@pytest.mark.parametrize("timeframe", ["1h", "4h"])
def test_closed_trend_is_measured_causally(timeframe: str) -> None:
    bars = _bars(timeframe=timeframe)
    context = detect_regime(bars)

    assert context.ready
    assert context.label is RegimeLabel.TRENDING_UP
    assert context.timeframe == timeframe
    assert context.trend_direction == "up"
    assert context.adx is not None and context.adx >= REGIME_CONFIG.trend_adx_min
    assert context.ema_slope_bps is not None and context.ema_slope_bps > 0
    assert context.reason == "ok"


def test_low_liquidity_has_priority_and_vector_is_frozen() -> None:
    bars = _bars()
    bars[-1] = replace(
        bars[-1],
        volume=Decimal(1),
        quote_volume=bars[-1].close,
        trade_count=1,
    )
    context = detect_regime(bars)

    assert context.label is RegimeLabel.LOW_LIQUIDITY
    assert context.volume_ratio is not None
    assert context.volume_ratio < REGIME_CONFIG.low_liquidity_ratio
    with pytest.raises(FrozenInstanceError):
        context.ready = False  # type: ignore[misc]


def test_forming_degraded_and_gapped_inputs_fail_closed() -> None:
    bars = _bars()
    forming = [*bars[:-1], replace(bars[-1], is_closed=False)]
    assert detect_regime(forming).reason == "forming_bar_present"

    degraded = detect_regime(bars, data_quality="degraded")
    assert not degraded.ready
    assert degraded.label is RegimeLabel.UNAVAILABLE

    gapped = [*bars[:10], *bars[11:]]
    result = detect_regime(gapped)
    assert not result.ready
    assert result.data_quality == "gap"
    assert result.reason == "non_consecutive_candles"


def test_measurement_is_prefix_invariant() -> None:
    bars = _bars(count=REGIME_CONFIG.warmup_bars + 20)
    cut = REGIME_CONFIG.warmup_bars + 5

    prefix = detect_regime(bars[:cut])
    repeated = detect_regime(list(bars[:cut]))

    assert prefix == repeated
    assert prefix.as_of == bars[cut - 1].close_time
