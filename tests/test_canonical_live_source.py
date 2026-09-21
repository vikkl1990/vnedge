"""Future live decision source reads the research lake without OHLC fallback."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.data.candles import (
    TF_SECONDS,
    Candle,
    CandleParquetStore,
    aggregate_candle_series,
)
from vnedge.research.canonical_input import CanonicalResearchStore
from vnedge.runtime.canonical_live_source import (
    CanonicalLiveDecisionSource,
    CanonicalLiveProofError,
    RegisteredCanonicalScannerSource,
)

DAY = datetime(2026, 9, 14, tzinfo=UTC)
SYMBOL = "BTC/USD:USD"


def _bar(timeframe, opened):
    seconds = TF_SECONDS[timeframe]
    return Candle(
        symbol=SYMBOL, timeframe=timeframe, open_time=opened,
        close_time=opened + timedelta(seconds=seconds),
        open=Decimal(100), high=Decimal(102), low=Decimal(99),
        close=Decimal(101), volume=Decimal(2),
        quote_volume=Decimal(201), trade_count=2,
        taker_buy_volume=Decimal(1), vwap=Decimal("100.5"),
    )


def _store(tmp_path, *, omit_1m=False, omit_5m=False, omit_prior_4h=False):
    store = CandleParquetStore(tmp_path, exchange="delta_india")
    prior = DAY - timedelta(days=1)
    base = tuple(
        _bar("1m", prior + timedelta(minutes=index))
        for index in range(24 * 60 + 4 * 60 + 15)
    )
    series = {"1m": base}
    for source_tf, target_tf in (
        ("1m", "5m"), ("5m", "15m"), ("15m", "1h"),
        ("1h", "4h"), ("4h", "1d"),
    ):
        series[target_tf] = aggregate_candle_series(
            SYMBOL, source_tf, target_tf, series[source_tf],
        )
    for timeframe, candles in series.items():
        filtered = [
            candle for candle in candles
            if not (
                (omit_1m and timeframe == "1m"
                 and candle.open_time == DAY + timedelta(hours=4, minutes=6))
                or
                (omit_5m and timeframe == "5m"
                 and candle.open_time == DAY + timedelta(hours=4, minutes=5))
                or (omit_prior_4h and timeframe == "4h"
                    and candle.open_time == prior + timedelta(hours=12))
            )
        ]
        store.upsert(filtered)
    return store


def test_canonical_live_hashes_match_same_research_records_as_of_close(tmp_path):
    store = _store(tmp_path)
    close = DAY + timedelta(hours=4, minutes=15)
    frame = CanonicalLiveDecisionSource(store).bind(SYMBOL, "15m", close)
    assert frame.decision.close_time == close
    assert frame.context_4h.close_time == DAY + timedelta(hours=4)
    assert frame.context_1d.close_time == DAY
    assert frame.hashes == {
        "15m": next(
            row.content_sha256 for row in store.read_records(SYMBOL, "15m")
            if row.close_time == close
        ),
        "4h": store.read_records(SYMBOL, "4h")[-1].content_sha256,
        "1d": store.read_records(SYMBOL, "1d")[0].content_sha256,
    }


@pytest.mark.parametrize("missing", ["1m", "5m", "4h"])
def test_incomplete_child_window_rejects_parent_without_ohlc_fallback(tmp_path, missing):
    store = _store(
        tmp_path, omit_1m=missing == "1m", omit_5m=missing == "5m",
        omit_prior_4h=missing == "4h",
    )
    close = DAY + timedelta(hours=4, minutes=15)
    with pytest.raises(CanonicalLiveProofError, match="incomplete"):
        CanonicalLiveDecisionSource(store).bind(SYMBOL, "15m", close)


def test_selected_official_ohlc_cannot_be_scanner_decision_bar(tmp_path):
    store = _store(tmp_path)
    opened = DAY + timedelta(hours=4)
    store.upsert([_bar("15m", opened)], source="official_delta_ohlc")
    with pytest.raises(CanonicalLiveProofError, match="unproven canonical"):
        CanonicalLiveDecisionSource(store).bind(
            SYMBOL, "15m", opened + timedelta(minutes=15),
        )


def test_registered_scanner_gets_same_lake_hash_and_asof_context_as_research(tmp_path):
    store = _store(tmp_path)
    close = DAY + timedelta(hours=4, minutes=15)
    runtime = RegisteredCanonicalScannerSource(
        CanonicalLiveDecisionSource(store),
        strategy_id="htf_regime_continuation_15m_v2__BTCUSD",
        symbol=SYMBOL,
    )

    class Strategy:
        strategy_id = "htf_regime_continuation_15m_v2__BTCUSD"

        def __init__(self):
            self.contexts = {}

        def bind_canonical_context(self, timeframe, frame):
            self.contexts[timeframe] = frame

    strategy = Strategy()
    frames = runtime.bind_strategy(
        strategy, close, decision_bars=8, context_bars=8,
    )
    research = CanonicalResearchStore(tmp_path).read_candles(
        "delta_india", SYMBOL, "15m",
    )
    research_row = research.loc[research["close_time"] == close].iloc[0]
    runtime_row = frames.decision.iloc[-1]
    assert runtime_row["content_sha256"] == research_row["content_sha256"]
    assert runtime_row[["open", "high", "low", "close"]].tolist() == pytest.approx(
        research_row[["open", "high", "low", "close"]].tolist()
    )
    assert set(strategy.contexts) == {"4h", "1d"}
    assert strategy.contexts["4h"].iloc[-1]["content_sha256"] == frames.proof.hashes["4h"]
    assert strategy.contexts["1d"].iloc[-1]["content_sha256"] == frames.proof.hashes["1d"]


def test_registered_scanner_source_refuses_wrong_symbol_and_unregistered_id(tmp_path):
    source = CanonicalLiveDecisionSource(_store(tmp_path))
    with pytest.raises(CanonicalLiveProofError, match="symbol outside"):
        RegisteredCanonicalScannerSource(
            source,
            strategy_id="htf_regime_continuation_15m_v2__BTCUSD",
            symbol="ETHUSD",
        )
    with pytest.raises(CanonicalLiveProofError, match="no registered"):
        RegisteredCanonicalScannerSource(
            source, strategy_id="not_registered", symbol=SYMBOL,
        )
