"""Causal weekly/daily/4h playbook permission.

This module classifies *closed* higher-timeframe candles.  It never emits an
order or a signal; callers may only use the result to deny scanner sides.
Weekly OHLC is derived from seven complete UTC daily candles so a forming week
can never leak into a 15-minute decision.  The V1 weekly VWAP is joined from a
separate, complete Delta trade-lake artifact; candle quote-volume and HLC3 are
not eligible substitutes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
import pandas as pd

WeeklyBias = Literal["up", "down", "range"]
WeeklyClassifier = Literal["vwap_structure_v1", "range_structure_v1"]
DailyLocation = Literal["discount", "mid", "premium"]
H4Direction = Literal["up", "down", "range"]
EmaState = Literal["up", "down", "range"]
MacdImpulse = Literal["on", "off", "fade"]
RsiZone = Literal["discount", "mid", "premium"]
PlaybookFamily = Literal["continuation", "mean_revert", "flat"]
RegimeState = PlaybookFamily
ScannerFamily = Literal["squeeze", "range", "failed_break", "htf", "bos", "session"]
Side = Literal["long", "short"]
AsOfBar = tuple[str, int]


@dataclass(frozen=True, slots=True)
class MarketRegimeConfig:
    weekly_classifier: WeeklyClassifier = "vwap_structure_v1"
    daily_location_bars: int = 14
    h4_ema_bars: int = 20
    min_complete_weeks: int = 3
    daily_ema_fast: int = 21
    daily_ema_slow: int = 50
    daily_ema_climate: int = 200
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    rsi_discount: float = 40.0
    rsi_premium: float = 70.0

    def __post_init__(self) -> None:
        if self.weekly_classifier not in {
            "vwap_structure_v1",
            "range_structure_v1",
        }:
            raise ValueError("unsupported weekly_classifier")
        if self.daily_location_bars < 3:
            raise ValueError("daily_location_bars must be at least 3")
        if self.h4_ema_bars < 3:
            raise ValueError("h4_ema_bars must be at least 3")
        if self.min_complete_weeks < 3:
            raise ValueError("min_complete_weeks must be at least 3")
        if not 2 <= self.daily_ema_fast < self.daily_ema_slow < self.daily_ema_climate:
            raise ValueError("daily EMA periods must satisfy 2 <= fast < slow < climate")
        if not 2 <= self.macd_fast < self.macd_slow:
            raise ValueError("MACD periods must satisfy 2 <= fast < slow")
        if self.macd_signal < 2:
            raise ValueError("MACD signal period must be at least 2")
        if self.rsi_period < 2:
            raise ValueError("RSI period must be at least 2")
        if not 0 < self.rsi_discount < self.rsi_premium < 100:
            raise ValueError("RSI zones must satisfy 0 < discount < premium < 100")


DEFAULT_CONFIG = MarketRegimeConfig()


@dataclass(frozen=True, slots=True)
class MarketRegime:
    weekly: WeeklyBias
    daily: DailyLocation
    h4: H4Direction
    allow_long: bool
    allow_short: bool
    state: RegimeState
    reason: str
    ready: bool = True
    as_of: pd.Timestamp | None = None
    asof_bar: AsOfBar | None = None
    exit_reason: str | None = None
    ema_state: EmaState = "range"
    macd_impulse: MacdImpulse = "off"
    rsi_zone: RsiZone = "mid"
    daily_ema21: float | None = None
    daily_ema50: float | None = None
    daily_ema200: float | None = None
    daily_macd_hist: float | None = None
    h4_macd_hist: float | None = None
    daily_rsi: float | None = None
    daily_observations: int = 0
    ema200_ready: bool = False

    @property
    def family(self) -> PlaybookFamily:
        """Compatibility alias; ``state`` is the canonical name."""
        return self.state

    def allows(self, side: Side, *, family: PlaybookFamily) -> bool:
        """Return a denial-only playbook permission."""
        if not self.ready or self.state != family:
            return False
        return self.allow_long if side == "long" else self.allow_short

    def allows_scanner(
        self,
        scanner_family: ScannerFamily,
        side: Side,
        *,
        pullback: bool = False,
    ) -> bool:
        """Deny scanners whose family or side conflicts with the telescope.

        This method never creates a setup.  The scanner still owns its closed
        decision-timeframe geometry and entry clock.
        """
        if not self.ready or self.state == "flat":
            return False
        side_allowed = self.allow_long if side == "long" else self.allow_short
        if not side_allowed:
            return False
        if scanner_family in {"htf", "bos", "session"}:
            return self.state == "continuation"
        if scanner_family in {"range", "failed_break"}:
            return self.state == "mean_revert"
        if scanner_family == "squeeze":
            return self.state == "mean_revert" or (
                self.state == "continuation" and pullback
            )
        return False


def _closed_frame(frame: pd.DataFrame, *, name: str) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{name} missing columns: {sorted(missing)}")
    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    if out["timestamp"].isna().any():
        raise ValueError(f"{name} requires valid UTC timestamps")
    for column in ("open", "high", "low", "close", "volume", "quote_volume"):
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    if "is_closed" in out.columns:
        out = out[out["is_closed"].eq(True)]
    if "data_quality" in out.columns:
        out = out[out["data_quality"].astype(str).str.lower().eq("ok")]
    return out.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(
        drop=True
    )


def complete_weeks_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    """Aggregate only seven-day, consecutive UTC weeks (Monday open)."""
    work = _closed_frame(daily, name="daily regime context")
    if work.empty:
        return pd.DataFrame()
    ts = work["timestamp"]
    work["week_open"] = ts.dt.floor("D") - pd.to_timedelta(ts.dt.dayofweek, unit="D")
    rows: list[dict[str, object]] = []
    one_day = pd.Timedelta(days=1)
    for week_open, group in work.groupby("week_open", sort=True):
        group = group.sort_values("timestamp")
        expected = pd.date_range(week_open, periods=7, freq="1D", tz="UTC")
        actual = pd.DatetimeIndex(group["timestamp"].dt.floor("D"))
        if len(group) != 7 or not actual.equals(expected):
            continue
        if group.iloc[-1]["timestamp"] + one_day != week_open + pd.Timedelta(days=7):
            continue
        volume_series = group.get("volume", pd.Series(np.nan, index=group.index))
        quote_series = group.get("quote_volume", pd.Series(np.nan, index=group.index))
        volume = float(volume_series.sum()) if volume_series.notna().all() else math.nan
        quote = float(quote_series.sum()) if quote_series.notna().all() else math.nan
        # Weekly value must remain the exact canonical quote/base ratio.  HLC3
        # is a different measurement and silently substituting it would let a
        # lower-quality exchange candle change the playbook permission.
        vwap = quote / volume if volume > 0 and quote > 0 else math.nan
        rows.append(
            {
                "timestamp": week_open,
                "open": float(group.iloc[0]["open"]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group.iloc[-1]["close"]),
                "volume": volume,
                "quote_volume": quote,
                "vwap": vwap,
                "is_closed": True,
            }
        )
    return pd.DataFrame(rows)


def _attach_trade_lake_weekly_vwap(
    weeks: pd.DataFrame,
    artifacts: pd.DataFrame | None,
    *,
    as_of: pd.Timestamp | None,
    expected_symbol: str | None,
) -> pd.DataFrame:
    """Attach only complete Delta trade-lake VWAP rows to weekly OHLC.

    Canonical daily candles may carry a ``quote_volume`` column on other
    venues, but V1's Delta contract explicitly refuses that fallback.  The
    derived artifact is a separately persisted accumulator and is read here;
    it is never recomputed in the regime/scanner loop.
    """
    out = weeks.copy()
    out["vwap"] = math.nan
    if artifacts is None or artifacts.empty or out.empty:
        return out
    required = {
        "exchange",
        "timeframe",
        "symbol",
        "open_time",
        "close_time",
        "vwap",
        "sum_base",
        "sum_notional",
        "n_trades",
        "source",
        "coverage_ok",
    }
    if required.difference(artifacts.columns):
        return out
    values = artifacts.copy()
    values["open_time"] = pd.to_datetime(values["open_time"], utc=True, errors="coerce")
    values["close_time"] = pd.to_datetime(values["close_time"], utc=True, errors="coerce")
    values["vwap"] = pd.to_numeric(values["vwap"], errors="coerce")
    values["sum_base"] = pd.to_numeric(values["sum_base"], errors="coerce")
    values["sum_notional"] = pd.to_numeric(
        values["sum_notional"], errors="coerce"
    )
    values["n_trades"] = pd.to_numeric(values["n_trades"], errors="coerce")
    exact_vwap = np.isclose(
        values["vwap"],
        values["sum_notional"] / values["sum_base"],
        rtol=1e-12,
        atol=1e-12,
        equal_nan=False,
    )
    values = values[
        values["exchange"].astype(str).str.lower().eq("delta_india")
        & values["timeframe"].astype(str).eq("1w")
        & values["source"].astype(str).eq("trade_lake")
        & values["coverage_ok"].eq(True)
        & values["vwap"].gt(0)
        & values["sum_base"].gt(0)
        & values["sum_notional"].gt(0)
        & values["n_trades"].gt(0)
        & exact_vwap
        & values["open_time"].notna()
        & values["close_time"].eq(values["open_time"] + pd.Timedelta(days=7))
    ]
    if expected_symbol is not None:
        values = values[values["symbol"].astype(str).eq(expected_symbol)]
    if as_of is not None:
        values = values[values["close_time"] <= as_of]
    if values.empty:
        return out
    lookup = (
        values.sort_values("close_time")
        .drop_duplicates("open_time", keep="last")
        .set_index("open_time")["vwap"]
    )
    out["vwap"] = pd.to_datetime(out["timestamp"], utc=True).map(lookup)
    return out


def _weekly_bias(weeks: pd.DataFrame, *, minimum: int) -> WeeklyBias | None:
    if len(weeks) < minimum:
        return None
    prior2, prior, last = weeks.iloc[-3], weeks.iloc[-2], weeks.iloc[-1]
    values = (
        last["close"],
        last["high"],
        last["low"],
        last["vwap"],
        prior["high"],
        prior["low"],
        prior2["high"],
        prior2["low"],
    )
    if not all(np.isfinite(float(value)) for value in values):
        return None
    breakout_up = float(last["close"]) > float(prior["high"])
    breakout_down = float(last["close"]) < float(prior["low"])
    rising_lows = float(last["low"]) > float(prior["low"]) > float(prior2["low"])
    falling_highs = float(last["high"]) < float(prior["high"]) < float(prior2["high"])
    above_value = float(last["close"]) > float(last["vwap"])
    below_value = float(last["close"]) < float(last["vwap"])
    if breakout_up or (above_value and rising_lows):
        return "up"
    if breakout_down or (below_value and falling_highs):
        return "down"
    return "range"


def _weekly_range_structure_bias(
    weeks: pd.DataFrame, *, minimum: int
) -> WeeklyBias | None:
    """Classify weekly direction from official OHLC only.

    This is intentionally a separate classifier from the frozen VWAP
    contract.  Official Delta candle history has no quote volume, so it can
    support causal range/structure classification but cannot manufacture a
    weekly VWAP from HLC3.
    """
    if len(weeks) < minimum:
        return None
    prior2, prior, last = weeks.iloc[-3], weeks.iloc[-2], weeks.iloc[-1]
    values = (
        last["open"],
        last["close"],
        last["high"],
        last["low"],
        prior["high"],
        prior["low"],
        prior2["high"],
        prior2["low"],
    )
    if not all(np.isfinite(float(value)) for value in values):
        return None
    breakout_up = float(last["close"]) > float(prior["high"])
    breakout_down = float(last["close"]) < float(prior["low"])
    rising_lows = float(last["low"]) > float(prior["low"]) > float(prior2["low"])
    falling_highs = (
        float(last["high"]) < float(prior["high"]) < float(prior2["high"])
    )
    if breakout_up or (rising_lows and float(last["close"]) > float(last["open"])):
        return "up"
    if breakout_down or (
        falling_highs and float(last["close"]) < float(last["open"])
    ):
        return "down"

    # ``range`` is the conservative third state.  The explicit inside and
    # compression calculation documents the preferred mean-reversion shape;
    # ambiguous non-directional weeks remain range instead of becoming an
    # invented trend.
    prior_high = max(float(prior["high"]), float(prior2["high"]))
    prior_low = min(float(prior["low"]), float(prior2["low"]))
    last_range = float(last["high"]) - float(last["low"])
    prior_ranges = np.asarray(
        [
            float(prior["high"]) - float(prior["low"]),
            float(prior2["high"]) - float(prior2["low"]),
        ],
        dtype=float,
    )
    inside_compressed_range = (
        float(last["high"]) <= prior_high
        and float(last["low"]) >= prior_low
        and last_range <= float(np.median(prior_ranges))
    )
    if inside_compressed_range:
        return "range"
    return "range"


def _daily_location(daily: pd.DataFrame, *, bars: int) -> DailyLocation | None:
    if len(daily) < bars:
        return None
    window = daily.iloc[-bars:]
    low = float(window["low"].min())
    high = float(window["high"].max())
    close = float(window.iloc[-1]["close"])
    width = high - low
    if not np.isfinite(width) or width <= 0:
        return None
    position = (close - low) / width
    if position <= 1.0 / 3.0:
        return "discount"
    if position >= 2.0 / 3.0:
        return "premium"
    return "mid"


def _h4_direction(h4: pd.DataFrame, *, ema_bars: int) -> H4Direction | None:
    if len(h4) < ema_bars + 1:
        return None
    close = pd.to_numeric(h4["close"], errors="coerce")
    ema = close.ewm(span=ema_bars, adjust=False, min_periods=ema_bars).mean()
    current, previous = float(ema.iloc[-1]), float(ema.iloc[-2])
    last_close = float(close.iloc[-1])
    if not all(np.isfinite(value) for value in (current, previous, last_close)):
        return None
    if last_close > current > previous:
        return "up"
    if last_close < current < previous:
        return "down"
    return "range"


@dataclass(frozen=True, slots=True)
class _Telescope:
    ema_state: EmaState
    macd_impulse: MacdImpulse
    rsi_zone: RsiZone
    daily_ema_fast: float
    daily_ema_slow: float
    daily_ema_climate: float
    daily_macd_hist: float
    h4_macd_hist: float
    daily_rsi: float


def _macd_histogram(
    close: pd.Series, *, fast: int, slow: int, signal: int
) -> pd.Series:
    fast_ema = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_ema = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    line = fast_ema - slow_ema
    signal_line = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line - signal_line


def _rsi(close: pd.Series, *, period: int) -> pd.Series:
    change = close.diff()
    gain = change.clip(lower=0.0)
    loss = -change.clip(upper=0.0)
    avg_gain = gain.ewm(
        alpha=1.0 / period, adjust=False, min_periods=period
    ).mean()
    avg_loss = loss.ewm(
        alpha=1.0 / period, adjust=False, min_periods=period
    ).mean()
    relative = avg_gain / avg_loss.replace(0.0, np.nan)
    value = 100.0 - (100.0 / (1.0 + relative))
    value = value.mask(avg_loss.eq(0.0) & avg_gain.gt(0.0), 100.0)
    value = value.mask(avg_gain.eq(0.0) & avg_loss.gt(0.0), 0.0)
    return value.mask(avg_gain.eq(0.0) & avg_loss.eq(0.0), 50.0)


def _telescope_state(
    daily: pd.DataFrame,
    h4: pd.DataFrame,
    *,
    config: MarketRegimeConfig,
) -> _Telescope | None:
    if len(daily) < config.daily_ema_climate:
        return None
    macd_minimum = config.macd_slow + config.macd_signal
    if len(daily) < macd_minimum or len(h4) < macd_minimum:
        return None
    daily_close = pd.to_numeric(daily["close"], errors="coerce")
    h4_close = pd.to_numeric(h4["close"], errors="coerce")
    ema_fast = daily_close.ewm(
        span=config.daily_ema_fast,
        adjust=False,
        min_periods=config.daily_ema_fast,
    ).mean()
    ema_slow = daily_close.ewm(
        span=config.daily_ema_slow,
        adjust=False,
        min_periods=config.daily_ema_slow,
    ).mean()
    ema_climate = daily_close.ewm(
        span=config.daily_ema_climate,
        adjust=False,
        min_periods=config.daily_ema_climate,
    ).mean()
    daily_hist = _macd_histogram(
        daily_close,
        fast=config.macd_fast,
        slow=config.macd_slow,
        signal=config.macd_signal,
    )
    h4_hist = _macd_histogram(
        h4_close,
        fast=config.macd_fast,
        slow=config.macd_slow,
        signal=config.macd_signal,
    )
    rsi = _rsi(daily_close, period=config.rsi_period)
    values = (
        float(daily_close.iloc[-1]),
        float(ema_fast.iloc[-1]),
        float(ema_slow.iloc[-1]),
        float(ema_climate.iloc[-1]),
        float(daily_hist.iloc[-1]),
        float(daily_hist.iloc[-2]),
        float(h4_hist.iloc[-1]),
        float(h4_hist.iloc[-2]),
        float(rsi.iloc[-1]),
    )
    if not all(np.isfinite(value) for value in values):
        return None
    (
        close,
        fast,
        slow,
        climate,
        daily_current,
        daily_previous,
        h4_current,
        h4_previous,
        rsi_current,
    ) = values
    if close > fast > slow > climate:
        ema_state: EmaState = "up"
    elif close < fast < slow < climate:
        ema_state = "down"
    else:
        ema_state = "range"

    if ema_state == "up" and daily_current > 0 and h4_current > 0:
        macd_impulse: MacdImpulse = (
            "fade"
            if daily_current < daily_previous and h4_current < h4_previous
            else "on"
        )
    elif ema_state == "down" and daily_current < 0 and h4_current < 0:
        macd_impulse = (
            "fade"
            if daily_current > daily_previous and h4_current > h4_previous
            else "on"
        )
    elif (
        (ema_state == "up" and (daily_current > 0 or h4_current > 0))
        or (ema_state == "down" and (daily_current < 0 or h4_current < 0))
    ):
        macd_impulse = "fade"
    else:
        macd_impulse = "off"

    if rsi_current <= config.rsi_discount:
        rsi_zone: RsiZone = "discount"
    elif rsi_current >= config.rsi_premium:
        rsi_zone = "premium"
    else:
        rsi_zone = "mid"
    return _Telescope(
        ema_state=ema_state,
        macd_impulse=macd_impulse,
        rsi_zone=rsi_zone,
        daily_ema_fast=fast,
        daily_ema_slow=slow,
        daily_ema_climate=climate,
        daily_macd_hist=daily_current,
        h4_macd_hist=h4_current,
        daily_rsi=rsi_current,
    )


def _context_asof_bar(
    daily: pd.DataFrame,
    h4: pd.DataFrame,
    weeks: pd.DataFrame,
) -> AsOfBar | None:
    candidates: list[tuple[pd.Timestamp, int, str]] = []
    if not h4.empty:
        candidates.append(
            (pd.Timestamp(h4.iloc[-1]["timestamp"]) + pd.Timedelta(hours=4), 0, "4h")
        )
    if not daily.empty:
        candidates.append(
            (pd.Timestamp(daily.iloc[-1]["timestamp"]) + pd.Timedelta(days=1), 1, "1d")
        )
    if not weeks.empty:
        candidates.append(
            (pd.Timestamp(weeks.iloc[-1]["timestamp"]) + pd.Timedelta(days=7), 2, "1w")
        )
    if not candidates:
        return None
    closed_at, _, timeframe = max(candidates, key=lambda item: (item[0], item[1]))
    return timeframe, int(closed_at.timestamp())


def regime_from_closed(
    daily: pd.DataFrame,
    h4: pd.DataFrame,
    *,
    as_of: pd.Timestamp | None = None,
    weekly_vwap_artifacts: pd.DataFrame | None = None,
    weekly_vwap_symbol: str | None = None,
    config: MarketRegimeConfig = DEFAULT_CONFIG,
) -> MarketRegime:
    """Classify the last closed daily/weekly/4h state at ``as_of``.

    ``timestamp`` is the candle open. A row is available only after its full
    timeframe has elapsed. The result is a permission snapshot, never a price
    forecast or an order instruction.
    """
    daily_frame = _closed_frame(daily, name="daily regime context")
    h4_frame = _closed_frame(h4, name="4h regime context")
    cutoff = pd.Timestamp(as_of) if as_of is not None else None
    if cutoff is not None:
        cutoff = (
            cutoff.tz_localize("UTC")
            if cutoff.tzinfo is None
            else cutoff.tz_convert("UTC")
        )
        daily_frame = daily_frame[
            daily_frame["timestamp"] + pd.Timedelta(days=1) <= cutoff
        ].reset_index(drop=True)
        h4_frame = h4_frame[
            h4_frame["timestamp"] + pd.Timedelta(hours=4) <= cutoff
        ].reset_index(drop=True)
    weeks = complete_weeks_from_daily(daily_frame)
    if config.weekly_classifier == "range_structure_v1":
        weekly = _weekly_range_structure_bias(
            weeks, minimum=config.min_complete_weeks
        )
    else:
        trade_vwap_weeks = _attach_trade_lake_weekly_vwap(
            weeks,
            weekly_vwap_artifacts,
            as_of=cutoff,
            expected_symbol=weekly_vwap_symbol,
        )
        weekly = _weekly_bias(trade_vwap_weeks, minimum=config.min_complete_weeks)
    location = _daily_location(daily_frame, bars=config.daily_location_bars)
    h4_direction = _h4_direction(h4_frame, ema_bars=config.h4_ema_bars)
    telescope = _telescope_state(daily_frame, h4_frame, config=config)
    asof_bar = _context_asof_bar(daily_frame, h4_frame, weeks)
    resolved_as_of = cutoff
    if resolved_as_of is None and asof_bar is not None:
        resolved_as_of = pd.Timestamp(asof_bar[1], unit="s", tz="UTC")
    telescope_fields = (
        {
            "ema_state": telescope.ema_state,
            "macd_impulse": telescope.macd_impulse,
            "rsi_zone": telescope.rsi_zone,
            "daily_ema21": telescope.daily_ema_fast,
            "daily_ema50": telescope.daily_ema_slow,
            "daily_ema200": telescope.daily_ema_climate,
            "daily_macd_hist": telescope.daily_macd_hist,
            "h4_macd_hist": telescope.h4_macd_hist,
            "daily_rsi": telescope.daily_rsi,
        }
        if telescope is not None
        else {}
    )
    daily_climate_probe = pd.to_numeric(
        daily_frame.get("close", pd.Series(dtype=float)), errors="coerce"
    ).ewm(
        span=config.daily_ema_climate,
        adjust=False,
        min_periods=config.daily_ema_climate,
    ).mean()
    telescope_fields["daily_observations"] = len(daily_frame)
    telescope_fields["ema200_ready"] = bool(
        not daily_climate_probe.empty
        and np.isfinite(float(daily_climate_probe.iloc[-1]))
    )

    def resolved(
        *,
        allow_long: bool,
        allow_short: bool,
        state: RegimeState,
        reason: str,
        ready: bool = True,
        exit_reason: str | None = None,
    ) -> MarketRegime:
        return MarketRegime(
            weekly=weekly or "range",
            daily=location or "mid",
            h4=h4_direction or "range",
            allow_long=allow_long,
            allow_short=allow_short,
            state=state,
            reason=reason,
            ready=ready,
            as_of=resolved_as_of,
            asof_bar=asof_bar,
            exit_reason=exit_reason,
            **telescope_fields,
        )

    if weekly is None or location is None or h4_direction is None or telescope is None:
        missing: list[str] = []
        if weekly is None:
            missing.append("weekly")
        if location is None:
            missing.append("daily_location")
        if h4_direction is None:
            missing.append("h4_direction")
        if telescope is None:
            missing.append("daily_4h_telescope")
        return resolved(
            allow_long=False,
            allow_short=False,
            state="flat",
            reason="insufficient_closed_htf_context:" + ",".join(missing),
            ready=False,
            exit_reason="htf_bias_invalidated",
        )

    if weekly == "up" and h4_direction == "down":
        return resolved(
            allow_long=False,
            allow_short=False,
            state="flat",
            reason="htf_invalidated:4h_opposes_weekly_up",
            exit_reason="htf_bias_invalidated",
        )
    if weekly == "down" and h4_direction == "up":
        return resolved(
            allow_long=False,
            allow_short=False,
            state="flat",
            reason="htf_invalidated:4h_opposes_weekly_down",
            exit_reason="htf_bias_invalidated",
        )
    if weekly == "up" and telescope.ema_state == "down":
        return resolved(
            allow_long=False,
            allow_short=False,
            state="flat",
            reason="htf_invalidated:daily_ema_opposes_weekly_up",
            exit_reason="htf_bias_invalidated",
        )
    if weekly == "down" and telescope.ema_state == "up":
        return resolved(
            allow_long=False,
            allow_short=False,
            state="flat",
            reason="htf_invalidated:daily_ema_opposes_weekly_down",
            exit_reason="htf_bias_invalidated",
        )

    long_trend = (
        weekly != "down"
        and telescope.ema_state == "up"
        and h4_direction == "up"
        and telescope.macd_impulse == "on"
    )
    short_trend = (
        weekly != "up"
        and telescope.ema_state == "down"
        and h4_direction == "down"
        and telescope.macd_impulse == "on"
    )
    if long_trend:
        permitted = location != "premium" and telescope.rsi_zone != "premium"
        return resolved(
            allow_long=permitted,
            allow_short=False,
            state="continuation",
            reason=(
                "weekly_daily_4h_telescope_long"
                if permitted
                else "daily_rsi_or_range_premium_no_chase"
            ),
        )
    if short_trend:
        permitted = location != "discount" and telescope.rsi_zone != "discount"
        return resolved(
            allow_long=False,
            allow_short=permitted,
            state="continuation",
            reason=(
                "weekly_daily_4h_telescope_short"
                if permitted
                else "daily_rsi_or_range_discount_no_chase"
            ),
        )

    if weekly == "range" and telescope.macd_impulse == "off":
        allow_long = h4_direction != "down" and telescope.ema_state != "down"
        allow_short = h4_direction != "up" and telescope.ema_state != "up"
        if location == "discount" or telescope.rsi_zone == "discount":
            allow_short = False
        elif location == "premium" or telescope.rsi_zone == "premium":
            allow_long = False
        return resolved(
            allow_long=allow_long,
            allow_short=allow_short,
            state="mean_revert",
            reason="weekly_range_macd_off",
        )
    if telescope.macd_impulse == "fade":
        return resolved(
            allow_long=False,
            allow_short=False,
            state="flat",
            reason="daily_4h_macd_impulse_fade",
        )
    return resolved(
        allow_long=False,
        allow_short=False,
        state="flat",
        reason="telescope_not_aligned_with_playbook",
    )


def _continuation_side(regime: MarketRegime) -> Side | None:
    if regime.ema_state == "up" and regime.h4 == "up" and regime.weekly != "down":
        return "long"
    if regime.ema_state == "down" and regime.h4 == "down" and regime.weekly != "up":
        return "short"
    return None


class MarketRegimeMachine:
    """Stateful hysteresis over causal higher-timeframe snapshots.

    Repeated 15m calls with the same ``asof_bar`` cannot change the state.
    Only a newly closed 4h/daily/weekly context bar may transition it.
    """

    def __init__(self, config: MarketRegimeConfig = DEFAULT_CONFIG) -> None:
        self.config = config
        self.current: MarketRegime | None = None

    def step(
        self,
        daily: pd.DataFrame,
        h4: pd.DataFrame,
        *,
        as_of: pd.Timestamp | None = None,
        weekly_vwap_artifacts: pd.DataFrame | None = None,
        weekly_vwap_symbol: str | None = None,
        data_healthy: bool = True,
        health_reason: str = "data_unhealthy",
    ) -> MarketRegime:
        if not data_healthy:
            resolved_as_of = pd.Timestamp(as_of) if as_of is not None else None
            if resolved_as_of is not None:
                resolved_as_of = (
                    resolved_as_of.tz_localize("UTC")
                    if resolved_as_of.tzinfo is None
                    else resolved_as_of.tz_convert("UTC")
                )
            candidate = MarketRegime(
                weekly=self.current.weekly if self.current is not None else "range",
                daily=self.current.daily if self.current is not None else "mid",
                h4=self.current.h4 if self.current is not None else "range",
                allow_long=False,
                allow_short=False,
                state="flat",
                reason=f"data_unhealthy:{health_reason}",
                ready=False,
                as_of=resolved_as_of,
                asof_bar=self.current.asof_bar if self.current is not None else None,
                exit_reason="htf_bias_invalidated",
            )
        else:
            candidate = regime_from_closed(
                daily,
                h4,
                as_of=as_of,
                weekly_vwap_artifacts=weekly_vwap_artifacts,
                weekly_vwap_symbol=weekly_vwap_symbol,
                config=self.config,
            )
        previous = self.current
        if previous is None:
            self.current = candidate
            return candidate
        if candidate.asof_bar == previous.asof_bar and previous.ready == candidate.ready:
            return previous

        if previous.state == "continuation":
            prior_side = _continuation_side(previous)
            if candidate.state == "mean_revert":
                candidate = replace(
                    candidate,
                    reason="expansion_spent",
                    exit_reason="htf_bias_invalidated",
                )
            elif candidate.state == "flat":
                extreme_fade = candidate.macd_impulse == "fade" and (
                    (prior_side == "long" and candidate.rsi_zone == "premium")
                    or (prior_side == "short" and candidate.rsi_zone == "discount")
                )
                if candidate.reason.startswith("htf_invalidated:") or extreme_fade:
                    candidate = replace(
                        candidate,
                        reason="htf_invalidated",
                        exit_reason="htf_bias_invalidated",
                    )
                elif prior_side is not None and _continuation_side(candidate) == prior_side:
                    candidate = replace(
                        candidate,
                        state="continuation",
                        allow_long=(
                            prior_side == "long" and candidate.rsi_zone != "premium"
                        ),
                        allow_short=(
                            prior_side == "short" and candidate.rsi_zone != "discount"
                        ),
                        reason="continuation_hysteresis",
                        exit_reason=None,
                    )
        elif previous.state == "mean_revert" and candidate.state == "continuation":
            candidate = replace(candidate, reason="range_expansion")

        self.current = candidate
        return candidate


__all__ = [
    "DEFAULT_CONFIG",
    "AsOfBar",
    "EmaState",
    "MacdImpulse",
    "MarketRegime",
    "MarketRegimeConfig",
    "MarketRegimeMachine",
    "RegimeState",
    "RsiZone",
    "ScannerFamily",
    "WeeklyClassifier",
    "complete_weeks_from_daily",
    "regime_from_closed",
]
