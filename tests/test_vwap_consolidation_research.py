"""Regression tests for the frozen, non-operational VWAP research harness."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from vnedge.data.bar_identity import bar_content_sha256

PATH = Path(__file__).resolve().parents[1] / 'research/reports/vwap_consolidation_20260914/replay.py'
spec = importlib.util.spec_from_file_location('vwap_ablation_test', PATH)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def fixture() -> pd.DataFrame:
    index = pd.date_range(replay.START, periods=60, freq='5min')
    rows = []
    for i, ts in enumerate(index):
        close = 101.1 if i == 35 else 100.
        volume = 2. if i == 35 else 1.
        row = dict(open_time=ts, close_time=ts+replay.STEP, open=100., high=101.2,
                   low=99., close=close, volume=volume, quote_volume=close*volume,
                   trade_count=10, source='canonical_tick_lake', candle_source='canonical_tick_lake',
                   is_closed=True, data_quality='ok', coverage_ok=True, eligible=True,
                   exchange='delta_india', symbol='BTCUSD', timeframe='5m')
        if i != 35:
            row['high'] = 101.
        row['content_sha256'] = bar_content_sha256(row, open_time=ts.to_pydatetime(),
            close_time=(ts+replay.STEP).to_pydatetime(), source='canonical_tick_lake')
        rows.append(row)
    return pd.DataFrame(rows,index=index)


def test_four_variants_bound_and_prefix_causal() -> None:
    f = fixture()
    all_events, _ = replay.generate(f,'BTCUSD')
    prefix_events, _ = replay.generate(f.iloc[:36],'BTCUSD')
    assert all_events == prefix_events
    assert len(all_events) == 1
    event = all_events[0]
    assert event['volume_ok'] and event['vwap_ok']
    assert set(event['envelopes']) == set(replay.VARIANTS)
    assert len({e['decision_id'] for e in event['envelopes'].values()}) == 4


def test_session_gap_invalidates_rest_of_day() -> None:
    f = fixture()
    f.loc[f.index[5], 'eligible'] = False
    assert not replay.prepare(f).session_ok.iloc[5:].any()
    assert replay.generate(f,'BTCUSD')[0] == []


def test_vwap_is_not_close_volume_proxy() -> None:
    f = fixture().iloc[:2].copy()
    f['volume'] = [1.,3.]
    f['quote_volume'] = [90.,330.]
    f['close'] = [1.,1.]
    assert replay.prepare(f).session_vwap.iloc[-1] == 105.


def test_next_open_stop_first_and_funding_unknown() -> None:
    at = replay.START + pd.Timedelta(hours=3)
    event = dict(decision_close=at.isoformat(),stop_price=99.,target_price=102.)
    minutes = pd.DataFrame([dict(open=100.,high=103.,low=98.,close=101.,eligible=True)],index=[at])
    result = replay.simulate(event,minutes)
    assert result['entry_proxy'] == 100
    assert result['exit_proxy'] == 99
    assert result['exit_reason'] == 'stop_tie'
    assert result['net_bps'] is None
    assert result['net_before_funding_bps'] < result['gross_bps']
    assert result['stress_before_funding_bps'] < result['net_before_funding_bps']


def test_missing_future_censored_not_zero() -> None:
    at = replay.START
    event = dict(decision_close=at.isoformat(),stop_price=99.,target_price=102.)
    minutes = pd.DataFrame([dict(open=100.,high=101.,low=99.5,close=100.,eligible=True)],index=[at])
    assert replay.simulate(event,minutes)['status'] == 'censored_missing_path'


def test_flat_price_cost_cash_not_double_subtracted() -> None:
    assert replay.cash_return(100.,100.) == pytest.approx(-17.7946616,abs=1e-5)
    assert replay.cash_return(100.,100.,0) == pytest.approx(-11.8)
