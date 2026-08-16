from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from vnedge.data.volume_profile import (
    TickLakeVolumeProfileStore,
    VolumeProfileArtifactStore,
    point_of_control,
    profile_location,
    volume_profile,
)

D = Decimal
START = datetime(2026, 8, 15, tzinfo=UTC)


def test_point_of_control_is_volume_mode_with_stable_lower_tie_break() -> None:
    prices = [D("100.1"), D("100.9"), D("101.1"), D("102.1")]
    volumes = [D("2"), D("3"), D("5"), D("1")]

    assert point_of_control(prices, volumes, D("1")) == D("100.5")


def test_profile_expands_value_area_from_poc_and_classifies_location() -> None:
    profile = volume_profile(
        [D("100.2"), D("101.2"), D("102.2")],
        [D("2"), D("10"), D("3")],
        D("1"),
        window_start=START,
        window_end=START + timedelta(days=1),
    )

    assert profile.poc == D("101.5")
    assert profile.value_area_low == D("101")
    assert profile.value_area_high == D("103")
    assert profile.total_volume == D("15")
    assert profile.trade_count == 3
    assert profile.realized_value_area_fraction == D("13") / D("15")
    assert profile_location(D("102"), profile) == "inside_value"
    assert profile_location(D("104"), profile) == "above_value"
    assert profile_location(D("99"), profile) == "below_value"
    assert profile_location(D("103"), profile) == "at_value_edge"


def test_value_area_uses_frozen_upper_tie_rule() -> None:
    profile = volume_profile(
        [D("99.2"), D("100.2"), D("101.2")],
        [D("5"), D("10"), D("5")],
        D("1"),
        window_start=START,
        window_end=START + timedelta(days=1),
    )

    assert profile.value_area_low == D("100")
    assert profile.value_area_high == D("102")
    assert profile.realized_value_area_fraction == D("0.75")


def test_profile_rejects_bad_contract_and_skips_invalid_observations() -> None:
    with pytest.raises(ValueError, match="equal length"):
        point_of_control([D("100")], [], D("1"))
    with pytest.raises(ValueError, match="bin_size"):
        point_of_control([D("100")], [D("1")], D("0"))

    profile = volume_profile(
        [D("100"), D("NaN"), D("101")],
        [D("2"), D("10"), D("-1")],
        D("1"),
        window_start=START,
        window_end=START + timedelta(days=1),
    )
    assert profile.trade_count == 1
    assert profile.poc == D("100.5")


def test_tick_lake_store_uses_closed_utc_window_and_caches_shards(tmp_path) -> None:
    directory = (
        tmp_path
        / "ticks/exchange=binanceusdm/symbol=BTCUSDT/stream=trades/20260815"
    )
    directory.mkdir(parents=True)
    frame = pd.DataFrame(
        [
            {"ts_ms": int((START + timedelta(hours=1)).timestamp() * 1000), "price": 100.2, "amount": 2.0},
            {"ts_ms": int((START + timedelta(hours=2)).timestamp() * 1000), "price": 101.2, "amount": 5.0},
            # The half-open window must not consume the next UTC day.
            {"ts_ms": int((START + timedelta(days=1)).timestamp() * 1000), "price": 999.0, "amount": 99.0},
        ]
    )
    frame.to_parquet(directory / "trades.parquet", index=False)
    store = TickLakeVolumeProfileStore(tmp_path)

    first = store.read(
        "binanceusdm",
        "BTCUSDT",
        START,
        START + timedelta(days=1),
        D("1"),
    )
    second = store.read(
        "binanceusdm",
        "BTCUSDT",
        START,
        START + timedelta(days=1),
        D("1"),
    )

    assert first is second
    assert first is not None
    assert first.poc == D("101.5")
    assert first.trade_count == 2


def test_artifact_store_persists_stable_measurement_contract(tmp_path) -> None:
    profile = volume_profile(
        [D("100.2"), D("101.2")],
        [D("2"), D("8")],
        D("1"),
        window_start=START,
        window_end=START + timedelta(days=1),
    )
    store = VolumeProfileArtifactStore(tmp_path / "profiles")

    window_id, path = store.put(
        exchange="binanceusdm",
        symbol="BTCUSDT",
        window_kind="prior_utc_day",
        source_exchange="binanceusdm_hist",
        profile=profile,
    )
    payload = json.loads(path.read_text())

    assert window_id == "binanceusdm:BTCUSDT:prior_utc_day:2026-08-15"
    assert payload["window_id"] == window_id
    assert payload["profile"]["poc"] == 101.5
    assert payload["profile"]["val"] == 101.0
    assert payload["profile"]["vah"] == 102.0
    assert payload["measurement_only"] is True
    assert payload["can_trade"] is False

    with pytest.raises(ValueError, match="invalid exchange"):
        store.path_for("../../escape", "BTCUSDT", "prior_utc_day", START)
