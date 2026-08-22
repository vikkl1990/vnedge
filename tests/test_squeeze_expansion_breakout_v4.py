from __future__ import annotations

import pandas as pd

from vnedge.strategy.squeeze_expansion_breakout_v4 import (
    SqueezeExpansionBreakoutV4,
)


class _PreparedFeatures:
    def __init__(self, *, compressed: float = 1.0, volume_ok: float = 1.0) -> None:
        self.compressed = compressed
        self.volume_ok = volume_ok

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = candles.copy()
        out["sqz_compressed"] = self.compressed
        out["sqz_volume_ok"] = self.volume_ok
        out["sqz_vwap24"] = -1.0
        return out


def _candles(rows: int = 290) -> pd.DataFrame:
    volume = pd.Series(range(1, rows + 1), dtype=float)
    price = pd.Series([100.0 + i / 10 for i in range(rows)])
    return pd.DataFrame(
        {
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price,
            "volume": volume,
            "quote_volume": volume * price,
            "trade_count": 10,
            "data_quality": "ok",
            "is_closed": True,
        }
    )


def test_v4_uses_canonical_quote_over_base_vwap_and_arms() -> None:
    strategy = SqueezeExpansionBreakoutV4()
    strategy._features = _PreparedFeatures()
    candles = _candles()

    out = strategy.prepare(candles)
    row = out.iloc[-1]
    prior = candles.iloc[-289:-1]
    expected = prior["quote_volume"].sum() / prior["volume"].sum()

    assert row["sqz_exact_volume_ready"] == 1.0
    assert row["sqz_arm_ready"] == 1.0
    assert row["sqz_compressed"] == 1.0
    assert row["sqz_vwap_source"] == "canonical_quote_over_base"
    assert abs(float(row["sqz_vwap24"]) - expected) < 1e-12


def test_v4_fails_closed_when_exact_window_has_a_gap() -> None:
    strategy = SqueezeExpansionBreakoutV4()
    strategy._features = _PreparedFeatures()
    candles = _candles()
    candles.loc[100, "data_quality"] = "gap"

    out = strategy.prepare(candles)

    assert out.iloc[-1]["sqz_exact_volume_ready"] == 0.0
    assert out.iloc[-1]["sqz_arm_ready"] == 0.0
    assert pd.isna(out.iloc[-1]["sqz_vwap24"])


def test_v4_makes_volume_confirmation_binding() -> None:
    strategy = SqueezeExpansionBreakoutV4()
    strategy._features = _PreparedFeatures(volume_ok=0.0)

    row = strategy.prepare(_candles()).iloc[-1]

    assert row["sqz_exact_volume_ready"] == 1.0
    assert row["sqz_arm_ready"] == 0.0
    assert row["sqz_compressed"] == 0.0


def test_v4_never_falls_back_to_close_volume_proxy() -> None:
    strategy = SqueezeExpansionBreakoutV4()
    strategy._features = _PreparedFeatures()
    candles = _candles().drop(columns=["quote_volume", "trade_count"])

    row = strategy.prepare(candles).iloc[-1]

    assert row["sqz_vwap_source"] == "unavailable"
    assert row["sqz_arm_ready"] == 0.0
    assert pd.isna(row["sqz_vwap24"])
