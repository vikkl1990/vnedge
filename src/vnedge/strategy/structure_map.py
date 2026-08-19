"""Structure map — S/R zones, order blocks, liquidity pools, VWAP bands.

A faithful port of the Structure Bounce detection stack, adapted to VNEDGE's
causality contract and micro-capital realities.  Every function reads only
bars at or before the evaluation index; nothing peeks forward.

Three deliberate changes from the source implementation:

* **No price rounding.**  The original rounds every level to 2 decimals, which
  is harmless on BTC and destroys levels on any instrument trading under a
  dollar.  Levels keep full precision; presentation can round.
* **One ATR source.**  The original can build zone widths from the primary
  timeframe's ATR while taking pivots from the confirm timeframe, producing
  zones ~4x too narrow whenever the confirm ATR is missing.  ATR is passed in
  explicitly and used consistently.
* **No blanket ``except Exception``.**  Bad inputs are rejected at the edge and
  return empty structure, so a bug surfaces instead of silently disabling the
  scanner.

Levels are *zones*, not prices: a level carries ``zone_low``/``zone_high`` and
membership is a band test.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import fmean, pstdev

# bars are (open_time_ms, open, high, low, close, volume)
Bar = tuple[int, float, float, float, float, float]

SUPPORT = "support"
RESISTANCE = "resistance"


@dataclass(frozen=True, slots=True)
class StructureLevel:
    price: float
    level_type: str          # "sr" | "order_block" | "liquidity" | "vwap_band"
    side: str                # SUPPORT | RESISTANCE
    strength: int            # 0-100
    zone_high: float
    zone_low: float
    touch_count: int = 0
    last_touch_bars_ago: int = 999
    extra: dict = field(default_factory=dict)

    @property
    def zone_width(self) -> float:
        return self.zone_high - self.zone_low

    def contains(self, price: float) -> bool:
        return self.zone_low <= price <= self.zone_high


@dataclass(frozen=True, slots=True)
class StructureMap:
    levels: tuple[StructureLevel, ...]
    nearest_support: StructureLevel | None
    nearest_resistance: StructureLevel | None
    vwap: float
    vwap_upper_1: float
    vwap_lower_1: float
    vwap_upper_2: float
    vwap_lower_2: float


def find_swings(
    bars: Sequence[Bar], lookback: int = 100
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """3-bar pivots. A pivot at i is only knowable once bar i+1 has closed."""
    n = min(lookback, len(bars) - 2)
    if n < 5:
        return [], []
    start = len(bars) - n
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(max(start + 1, 1), len(bars) - 1):
        if bars[i][2] > bars[i - 1][2] and bars[i][2] > bars[i + 1][2]:
            highs.append((i, bars[i][2]))
        if bars[i][3] < bars[i - 1][3] and bars[i][3] < bars[i + 1][3]:
            lows.append((i, bars[i][3]))
    return highs, lows


def find_horizontal_sr(
    bars: Sequence[Bar], atr: float, *, lookback: int = 100,
    min_touches: int = 2, zone_atr_frac: float = 0.3,
) -> list[StructureLevel]:
    """Zones where price pivoted at least ``min_touches`` times."""
    if len(bars) < 20 or atr <= 0:
        return []
    zone_width = atr * zone_atr_frac
    current = bars[-1][4]
    total = len(bars)

    highs, lows = find_swings(bars, lookback)
    pivots = [(i, p, "high") for i, p in highs] + [(i, p, "low") for i, p in lows]
    if not pivots:
        return []
    pivots.sort(key=lambda x: x[1])

    zones: list[dict] = []
    for index, price, kind in pivots:
        for zone in zones:
            if abs(price - zone["center"]) <= zone_width:
                zone["touches"].append((index, price, kind))
                zone["center"] = fmean(t[1] for t in zone["touches"])
                break
        else:
            zones.append({"center": price, "touches": [(index, price, kind)]})

    levels: list[StructureLevel] = []
    for zone in zones:
        touches = zone["touches"]
        if len(touches) < min_touches:
            continue
        prices = [t[1] for t in touches]
        center = fmean(prices)
        bars_ago = total - 1 - max(t[0] for t in touches)
        recency = 20 if bars_ago < 15 else (10 if bars_ago < 40 else 0)
        levels.append(
            StructureLevel(
                price=center,
                level_type="sr",
                side=SUPPORT if center < current else RESISTANCE,
                strength=min(len(touches) * 20 + recency, 100),
                zone_high=max(prices) + zone_width * 0.2,
                zone_low=min(prices) - zone_width * 0.2,
                touch_count=len(touches),
                last_touch_bars_ago=bars_ago,
            )
        )
    return levels


def detect_order_blocks(
    bars: Sequence[Bar], atrs: Sequence[float], *, lookback: int = 50,
    min_impulse_atr: float = 1.5,
) -> list[StructureLevel]:
    """Last opposing candle before an impulse, still unmitigated as of now."""
    if len(bars) < 10:
        return []
    total = len(bars)
    n = min(lookback, len(bars) - 3)
    start = len(bars) - n
    levels: list[StructureLevel] = []

    for i in range(max(start, 1), len(bars) - 2):
        atr = atrs[i] if i < len(atrs) else 0.0
        if atr <= 0:
            continue
        body_next = abs(bars[i + 1][4] - bars[i + 1][1])
        impulse = body_next > atr * min_impulse_atr
        if not impulse:
            continue
        bars_ago = total - 1 - i
        strength = min(int(body_next / atr * 30), 80)

        bearish = bars[i][4] < bars[i][1]
        bullish_impulse = bars[i + 1][4] > bars[i + 1][1]
        if bearish and bullish_impulse:
            ob_low, ob_high = bars[i][3], max(bars[i][4], bars[i][1])
            # mitigated only by bars that have ALREADY closed
            if not any(bars[j][3] <= ob_high for j in range(i + 2, len(bars))):
                levels.append(StructureLevel(
                    price=(ob_low + ob_high) / 2, level_type="order_block",
                    side=SUPPORT, strength=strength, zone_high=ob_high,
                    zone_low=ob_low, last_touch_bars_ago=bars_ago,
                    extra={"ob_type": "bullish", "impulse_atr": body_next / atr},
                ))

        bullish = bars[i][4] > bars[i][1]
        bearish_impulse = bars[i + 1][4] < bars[i + 1][1]
        if bullish and bearish_impulse:
            ob_high, ob_low = bars[i][2], min(bars[i][1], bars[i][4])
            if not any(bars[j][2] >= ob_low for j in range(i + 2, len(bars))):
                levels.append(StructureLevel(
                    price=(ob_low + ob_high) / 2, level_type="order_block",
                    side=RESISTANCE, strength=strength, zone_high=ob_high,
                    zone_low=ob_low, last_touch_bars_ago=bars_ago,
                    extra={"ob_type": "bearish", "impulse_atr": body_next / atr},
                ))
    return levels


def find_liquidity_zones(
    bars: Sequence[Bar], *, lookback: int = 100, equal_threshold_pct: float = 0.15,
) -> list[StructureLevel]:
    """Clusters of near-equal swing highs/lows -- resting liquidity."""
    highs, lows = find_swings(bars, lookback)
    current = bars[-1][4]
    total = len(bars)
    levels: list[StructureLevel] = []

    def _pairs(points: list[tuple[int, float]], side: str) -> None:
        for a in range(len(points)):
            for b in range(a + 1, len(points)):
                i_a, p_a = points[a]
                i_b, p_b = points[b]
                if p_a <= 0:
                    continue
                diff_pct = abs(p_a - p_b) / p_a * 100
                if diff_pct >= equal_threshold_pct:
                    continue
                bars_ago = total - 1 - max(i_a, i_b)
                strength = (70 if diff_pct < 0.05 else 50) + (15 if bars_ago < 20 else 0)
                if side == SUPPORT:
                    level_price = min(p_a, p_b)
                    if level_price >= current:
                        continue
                    zone_high, zone_low = max(p_a, p_b), level_price - current * 0.001
                else:
                    level_price = max(p_a, p_b)
                    if level_price <= current:
                        continue
                    zone_high, zone_low = level_price + current * 0.001, min(p_a, p_b)
                levels.append(StructureLevel(
                    price=level_price, level_type="liquidity", side=side,
                    strength=min(strength, 100), zone_high=zone_high, zone_low=zone_low,
                    touch_count=2, last_touch_bars_ago=bars_ago,
                    extra={"liq_type": "equal_lows" if side == SUPPORT else "equal_highs"},
                ))

    if len(lows) >= 2:
        _pairs(lows, SUPPORT)
    if len(highs) >= 2:
        _pairs(highs, RESISTANCE)
    return levels


def vwap_bands(bars: Sequence[Bar], *, window: int = 96) -> tuple[float, float, float, float, float]:
    """Rolling VWAP with +/-1 and +/-2 sigma bands.

    The source uses a cumulative VWAP with a fixed 50-bar deviation window, so
    the bands widen as the frame grows for reasons unrelated to volatility.
    A rolling window keeps sigma meaning what it appears to mean.
    """
    if len(bars) < 20:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    recent = bars[-window:]
    volume = sum(b[5] for b in recent)
    if volume <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    vwap = sum(((b[2] + b[3] + b[4]) / 3) * b[5] for b in recent) / volume
    deviations = [b[4] - vwap for b in recent]
    sigma = pstdev(deviations) if len(deviations) > 1 else 0.0
    return vwap, vwap + sigma, vwap - sigma, vwap + 2 * sigma, vwap - 2 * sigma


def build_structure_map(
    bars: Sequence[Bar], atrs: Sequence[float], *, atr: float,
    sr_lookback: int = 100, ob_lookback: int = 50, liq_lookback: int = 100,
    vwap_window: int = 96,
) -> StructureMap:
    """Merge every detector into one map, nearest levels resolved."""
    if len(bars) < 20 or atr <= 0:
        return StructureMap((), None, None, 0.0, 0.0, 0.0, 0.0, 0.0)
    current = bars[-1][4]
    levels: list[StructureLevel] = []
    levels += find_horizontal_sr(bars, atr, lookback=sr_lookback)
    levels += detect_order_blocks(bars, atrs, lookback=ob_lookback)
    levels += find_liquidity_zones(bars, lookback=liq_lookback)

    vwap, up1, lo1, up2, lo2 = vwap_bands(bars, window=vwap_window)
    if vwap > 0:
        band = abs(up1 - vwap) * 0.1
        if lo1 < current:
            levels.append(StructureLevel(price=lo1, level_type="vwap_band",
                                         side=SUPPORT, strength=60,
                                         zone_high=lo1 + band, zone_low=lo1 - band))
        if up1 > current:
            levels.append(StructureLevel(price=up1, level_type="vwap_band",
                                         side=RESISTANCE, strength=60,
                                         zone_high=up1 + band, zone_low=up1 - band))

    levels.sort(key=lambda level: abs(level.price - current))
    supports = [x for x in levels if x.side == SUPPORT and x.price < current]
    resistances = [x for x in levels if x.side == RESISTANCE and x.price > current]
    return StructureMap(
        levels=tuple(levels),
        nearest_support=supports[0] if supports else None,
        nearest_resistance=resistances[0] if resistances else None,
        vwap=vwap, vwap_upper_1=up1, vwap_lower_1=lo1,
        vwap_upper_2=up2, vwap_lower_2=lo2,
    )
