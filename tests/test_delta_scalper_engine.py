"""Delta scalper engine contracts, costs, causality, and safety routing."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import AccountState, PreTradeRiskGateway
from vnedge.scalping.delta_engine.backtester import CausalScalperBacktester
from vnedge.scalping.delta_engine.candle_store import (
    ClosedCandleAggregator,
    MultiTimeframeCandleStore,
)
from vnedge.scalping.delta_engine.config import DeltaScalperConfig, load_delta_scalper_config
from vnedge.scalping.delta_engine.context import MarketContextBuilder
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.scanners import MomentumBurstScanner, Scanner
from vnedge.scalping.delta_engine.signal_generator import (
    DeltaScalperSignalGenerator,
    EngineDecision,
    ScalperRiskAdapter,
    SignalGateConfig,
)
from vnedge.scalping.delta_engine.types import (
    Candle,
    L2Confirmation,
    MarketContext,
    Regime,
    Side,
    SignalCandidate,
)
from vnedge.scalping.microstructure import MarketMicroState, PrivateStreamState, TopOfBook
from vnedge.scalping.risk import ScalperRiskGateway

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def candle(ts: datetime, *, close: float = 100.0, tf: str = "1m") -> Candle:
    return Candle(ts, close - 0.2, close + 0.5, close - 0.5, close, 100.0, tf)


@pytest.mark.parametrize(
    ("deto", "maker", "hold", "expected_fee", "eligible"),
    [
        (False, True, 1_200, 2.36, True),
        (False, False, 1_200, 5.90, True),
        (True, True, 1_200, 1.77, True),
        (True, False, 1_200, 4.425, True),
        (False, True, 1_801, 8.26, False),
        (True, False, 1_801, 8.85, False),
    ],
)
def test_delta_fee_model_all_offer_and_deto_routes(deto, maker, hold, expected_fee, eligible):
    model = DeltaFeeModel(
        deto_enabled=deto,
        scalper_opted_in=True,
        default_slippage_bps_per_leg=0.0,
    )
    result = model.breakdown("BTCUSD", entry_is_maker=maker, hold_seconds=hold)
    assert result.total_bps == pytest.approx(expected_fee)
    assert result.scalper_eligible is eligible


def test_checked_in_config_is_valid_and_cannot_unlock_live():
    config = load_delta_scalper_config()
    assert config.engine.symbols == ("BTCUSD", "ETHUSD")
    assert not config.engine.live_orders_enabled
    with pytest.raises(ValueError, match="research-only"):
        DeltaScalperConfig.model_validate(
            {
                "engine": {
                    "mode": "live",
                    "live_orders_enabled": True,
                }
            }
        )


def test_scalper_offer_is_never_assumed_without_opt_in():
    result = DeltaFeeModel(
        scalper_opted_in=False, default_slippage_bps_per_leg=0
    ).breakdown("ETHUSD", entry_is_maker=True, hold_seconds=300)
    assert not result.scalper_eligible
    assert result.total_bps == pytest.approx(2.36 + 5.90)


def test_candle_store_converts_delta_start_to_close_and_rejects_future():
    store = MultiTimeframeCandleStore()
    start = NOW - timedelta(minutes=1)
    row = [start.timestamp() * 1000, 99.0, 101.0, 98.0, 100.0, 25.0]
    assert store.from_delta_row("BTCUSD", "1m", row, observed_at=NOW)
    assert store.latest("BTCUSD", "1m").ts == NOW
    assert not store.from_delta_row("BTCUSD", "1m", row, observed_at=NOW)
    with pytest.raises(ValueError, match="not closed"):
        store.append_closed("BTCUSD", candle(NOW + timedelta(minutes=1)), observed_at=NOW)


def test_delta_rollover_is_close_proof_despite_small_local_clock_skew():
    store = MultiTimeframeCandleStore()
    start = NOW - timedelta(minutes=1)
    row = [start.timestamp() * 1000, 99.0, 101.0, 98.0, 100.0, 25.0]
    assert store.from_delta_row(
        "BTCUSD", "1m", row, observed_at=NOW - timedelta(milliseconds=500)
    )


def test_closed_candle_aggregator_emits_only_complete_higher_bar():
    agg = ClosedCandleAggregator(("5m",))
    emitted = []
    for minute in range(1, 6):
        emitted.extend(
            agg.on_one_minute(
                "BTCUSD",
                candle(NOW.replace(minute=0) + timedelta(minutes=minute), close=100 + minute),
            )
        )
    assert len(emitted) == 1
    assert emitted[0].tf == "5m"
    assert emitted[0].close == 105
    assert emitted[0].ts.minute == 5


class _NoSignalGenerator:
    def on_candle_closed(self, symbol, timeframe, now=None):
        return EngineDecision(symbol, now, None, (), ())


def test_backtester_reports_and_resets_on_missing_minute():
    store = MultiTimeframeCandleStore()
    rows = [
        candle(NOW),
        candle(NOW + timedelta(minutes=1)),
        candle(NOW + timedelta(minutes=3)),
    ]
    report = CausalScalperBacktester(_NoSignalGenerator(), DeltaFeeModel(), store).run(
        "BTCUSD", rows
    )
    assert report.missing_one_minute_bars == 1
    assert report.data_quality_pass is False


def test_market_context_is_closed_only_and_immutable():
    store = MultiTimeframeCandleStore()
    for i in range(40):
        ts = NOW - timedelta(hours=40 - i)
        store.append_closed("BTCUSD", candle(ts, close=100 + i, tf="1h"), observed_at=NOW)
    for i in range(30):
        ts = NOW - timedelta(minutes=15 * (30 - i))
        store.append_closed("BTCUSD", candle(ts, close=120 + i / 10, tf="15m"), observed_at=NOW)
    for i in range(35):
        ts = NOW - timedelta(minutes=35 - i)
        store.append_closed("BTCUSD", candle(ts, close=125 + i / 10), observed_at=NOW)
    context = MarketContextBuilder(store).build("BTCUSD", now=NOW)
    assert context.ts <= NOW
    with pytest.raises(TypeError):
        context.features["future"] = 1.0


def test_context_marks_old_l2_confirmation_stale():
    store = MultiTimeframeCandleStore()
    store.append_closed("BTCUSD", candle(NOW), observed_at=NOW)
    builder = MarketContextBuilder(store, max_l2_age_seconds=1)
    builder.update_l2_confirmation(
        "BTCUSD", imbalance=0.5, cvd=100, observed_at=NOW - timedelta(seconds=2)
    )
    context = builder.build("BTCUSD", now=NOW)
    assert context.l2.status == "stale"
    assert context.l2.used_for_signal is False


def _momentum_context(l2: float) -> MarketContext:
    rows = tuple(candle(NOW - timedelta(minutes=31 - i), close=100 + i * 0.01) for i in range(30))
    latest = Candle(NOW, 100.2, 101.4, 100.1, 101.3, 1_000.0, "1m")
    return MarketContext(
        symbol="BTCUSD",
        ts=NOW,
        candles={"1m": rows + (latest,)},
        regime=Regime.EXPANDING,
        funding_rate=0.0,
        funding_velocity=0.0,
        l2=L2Confirmation(imbalance=l2, cvd=l2 * 100, status="fresh", observed_at=NOW),
        features={
            "volume_z": 3.0,
            "body_ratio": 0.92,
            "body_direction": 1.0,
            "breakout_up_bps": 8.0,
            "breakout_down_bps": -8.0,
            "atr_bps": 28.0,
        },
    )


def test_l2_never_changes_momentum_trigger_or_side():
    scanner = MomentumBurstScanner(DeltaFeeModel(scalper_opted_in=True))
    agrees = scanner.evaluate(_momentum_context(0.9))
    opposes = scanner.evaluate(_momentum_context(-0.9))
    assert agrees is not None and opposes is not None
    assert agrees.side is opposes.side is Side.LONG
    assert agrees.fee_adjusted_expectancy_bps == opposes.fee_adjusted_expectancy_bps
    assert agrees.metadata["l2_confirmation"]["used_for_signal"] is False
    assert opposes.metadata["l2_confirmation"]["used_for_execution"] is False


def _candidate() -> SignalCandidate:
    return SignalCandidate(
        scanner_id="test_scanner",
        symbol="BTCUSD",
        side=Side.LONG,
        decision_ts=NOW,
        entry_price=100.0,
        stop_loss=99.9,
        take_profits=(100.2,),
        time_stop_seconds=1_680,
        expected_hold_seconds=600,
        expected_move_bps=20.0,
        raw_expectancy_bps=16.0,
        modeled_cost_bps=5.36,
        fee_adjusted_expectancy_bps=10.64,
        scalper_probability=0.75,
        confidence=0.8,
    )


class _StaticScanner(Scanner):
    scanner_id = "test_scanner"

    def evaluate(self, ctx):
        return replace(_candidate(), decision_ts=ctx.ts)

    def required_features(self):
        return ()


class _StaticBuilder:
    def build(self, symbol, now=None):
        ts = now or NOW
        return MarketContext(
            symbol=symbol,
            ts=ts,
            candles={"1m": (candle(ts),)},
            regime=Regime.QUIET,
            funding_rate=0,
            funding_velocity=0,
        )


def test_generator_journals_each_decision_key_once():
    generator = DeltaScalperSignalGenerator(
        _StaticBuilder(),
        (_StaticScanner(),),
        gates=SignalGateConfig(min_expectancy_bps=8, min_probability=0.7),
    )
    first = generator.on_candle_closed("BTCUSD", "1m", now=NOW)
    duplicate = generator.on_candle_closed("BTCUSD", "1m", now=NOW)
    assert first.selected is not None
    assert duplicate.selected is None and duplicate.duplicate


def test_backtest_rebases_exit_distances_on_next_open_fill():
    store = MultiTimeframeCandleStore()
    generator = DeltaScalperSignalGenerator(
        _StaticBuilder(),
        (_StaticScanner(),),
        gates=SignalGateConfig(min_expectancy_bps=8, min_probability=0.7),
    )
    fee = DeltaFeeModel(
        scalper_opted_in=True, default_slippage_bps_per_leg=0
    )
    rows = [
        Candle(NOW, 99.9, 100.1, 99.8, 100.0, 1, "1m"),
        Candle(
            NOW + timedelta(minutes=1),
            110.0,
            110.3,
            109.95,
            110.2,
            1,
            "1m",
        ),
    ]
    report = CausalScalperBacktester(generator, fee, store).run("BTCUSD", rows)
    assert len(report.trades) == 1
    assert report.trades[0].exit_reason == "target_1"
    assert report.trades[0].gross_bps == pytest.approx(20.0)


def test_risk_adapter_uses_existing_gateway_and_never_submits(tmp_path):
    base = PreTradeRiskGateway(
        RiskConfig(max_spread_bps=10, max_slippage_bps=10),
        KillSwitch(kill_file=tmp_path / "KILL"),
    )
    adapter = ScalperRiskAdapter(ScalperRiskGateway(base))
    top = TopOfBook("BTCUSD", 99.99, 100, 100.01, 100, NOW)
    micro = MarketMicroState(
        top=top,
        private=PrivateStreamState(NOW, connected=True),
        estimated_slippage_bps=1,
    )
    account = AccountState(1_000, 0, 1_000, 0)
    result = adapter.evaluate(
        _candidate(), notional_usd=100, account=account, market=micro, now=NOW
    )
    assert result.intent.time_in_force == "PO"
    assert result.risk.approved
    assert result.submitted is False
