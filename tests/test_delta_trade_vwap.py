from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from vnedge.data.delta_trade_vwap import (
    TradeVwapArtifactStore,
    build_delta_trade_vwap_buckets,
    roll_weekly_vwap_from_daily,
)
from vnedge.exchange.delta_contracts import DeltaContractSpec

START = datetime(2026, 8, 17, tzinfo=UTC)  # Monday
BTC = DeltaContractSpec(symbol="BTCUSD", contract_value=0.001, tick_size=0.5)


def _trades(rows: list[tuple[datetime, float, int]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_ms": int(ts.timestamp() * 1000),
                "price": price,
                "size_contracts": contracts,
            }
            for ts, price, contracts in rows
        ]
    )


def test_delta_contract_counts_are_converted_to_base_before_vwap() -> None:
    trades = _trades(
        [
            (START + timedelta(hours=1), 100_000, 10),
            (START + timedelta(hours=2), 100_001, 10),
        ]
    )
    artifacts = build_delta_trade_vwap_buckets(
        trades,
        spec=BTC,
        timeframe="1d",
        closed_through=START + timedelta(days=1),
        complete_bucket_opens=[START],
    )

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.sum_base == Decimal("0.020")
    assert artifact.sum_notional == Decimal("2000.010")
    assert artifact.vwap == Decimal("100000.5")


def test_size_as_coin_is_not_an_implicit_input_contract() -> None:
    trades = pd.DataFrame(
        [{"ts_ms": int(START.timestamp() * 1000), "price": 100_000, "amount": 10}]
    )
    with pytest.raises(ValueError, match="size_contracts"):
        build_delta_trade_vwap_buckets(
            trades,
            spec=BTC,
            timeframe="1d",
            closed_through=START + timedelta(days=1),
            complete_bucket_opens=[START],
        )


def test_empty_or_unproven_bucket_emits_no_artifact() -> None:
    assert (
        build_delta_trade_vwap_buckets(
            pd.DataFrame(columns=["ts_ms", "price", "size_contracts"]),
            spec=BTC,
            timeframe="1d",
            closed_through=START + timedelta(days=1),
            complete_bucket_opens=[START],
        )
        == []
    )
    assert (
        build_delta_trade_vwap_buckets(
            _trades([(START + timedelta(hours=1), 100_000, 1)]),
            spec=BTC,
            timeframe="1d",
            closed_through=START + timedelta(days=1),
            complete_bucket_opens=[],
        )
        == []
    )


def test_weekly_rollup_sums_daily_accumulators_and_store_is_idempotent(tmp_path) -> None:
    daily = []
    for day in range(7):
        open_time = START + timedelta(days=day)
        daily.extend(
            build_delta_trade_vwap_buckets(
                _trades([(open_time + timedelta(hours=1), 100_000 + day, 1)]),
                spec=BTC,
                timeframe="1d",
                closed_through=open_time + timedelta(days=1),
                complete_bucket_opens=[open_time],
            )
        )
    weekly = roll_weekly_vwap_from_daily(daily, closed_through=START + timedelta(days=7))

    assert len(weekly) == 1
    assert weekly[0].sum_base == Decimal("0.007")
    assert weekly[0].vwap == Decimal(100003)
    store = TradeVwapArtifactStore(tmp_path)
    path = store.upsert(weekly)
    assert path is not None
    store.upsert(weekly)
    assert len(store.read("BTCUSD", "1w")) == 1


def test_hlc3_is_not_trade_lake_vwap_on_a_trending_week() -> None:
    trades = _trades(
        [
            (START + timedelta(hours=1), 100, 100),
            (START + timedelta(days=6, hours=1), 200, 1),
        ]
    )
    artifact = build_delta_trade_vwap_buckets(
        trades,
        spec=BTC,
        timeframe="1w",
        closed_through=START + timedelta(days=7),
        complete_bucket_opens=[START],
    )[0]
    hlc3 = (200 + 100 + 200) / 3
    assert float(artifact.vwap) != pytest.approx(hlc3)
