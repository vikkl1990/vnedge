"""Causality and clock checks for the bounded pullback research matrix."""
import importlib.util
from pathlib import Path

import pandas as pd

from vnedge.data.bar_identity import bar_content_sha256

PATH = Path(__file__).resolve().parents[1]/'research/reports/vwap_pullback_20260914/replay.py'
SPEC = importlib.util.spec_from_file_location('pullback_research_test',PATH)
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)


def fixture() -> pd.DataFrame:
    rows = []
    index = pd.date_range(R.START,periods=60,freq='5min')
    for i,ts in enumerate(index):
        row = {'open_time':ts,'close_time':ts+R.STEP,'open':100.1,'high':100.8,'low':99.7,
               'close':100.2,'volume':1.,'quote_volume':100.,'trade_count':10,
               'source':'canonical_tick_lake','candle_source':'canonical_tick_lake',
               'is_closed':True,'data_quality':'ok','coverage_ok':True,'eligible':True,
               'exchange':'delta_india','symbol':'BTCUSD','timeframe':'5m'}
        if i == 20:
            row['low'] = 99.
        if 30 <= i <= 38:
            row['low'] = 100.
        if i == 33:
            row.update(open=100.3,high=100.4,low=99.9,close=100.1,volume=.5,quote_volume=50.)
        if i == 34:
            row.update(open=100.1,high=100.2,low=99.95,close=100.05,volume=.5,quote_volume=50.)
        if i == 35:
            row.update(open=100.1,close=100.7,volume=2.,quote_volume=200.8)
        if i == 38:
            row.update(close=100.8)
        row['content_sha256'] = bar_content_sha256(row,open_time=ts.to_pydatetime(),
            close_time=(ts+R.STEP).to_pydatetime(),source='canonical_tick_lake')
        rows.append(row)
    return pd.DataFrame(rows,index=index)


def test_matrix_size_and_origin_prefix_causality() -> None:
    f = fixture()
    assert len(R.cells()) == len({R.key(c) for c in R.cells()}) == 48
    events,_ = R.origins(f,'BTCUSD')
    prefix,_ = R.origins(f.iloc[:36],'BTCUSD')
    assert events == prefix
    assert len(events) == 1 and events[0]['origin_index'] == 35
    assert events[0]['contraction_ok'] and events[0]['expansion_ok']


def test_higher_low_is_not_visible_on_recovery() -> None:
    f = fixture()
    assert R.pivot_proof(f,35,35,'BTCUSD') == []
    proof = R.pivot_proof(f,35,38,'BTCUSD')
    assert len(proof) == 2
    assert pd.Timestamp(proof[1]['anchor_open']) == f.index[33]
    assert pd.Timestamp(proof[1]['confirmed_at']) == f.index[36]+R.STEP


def test_delayed_envelope_binds_delayed_bar_not_origin() -> None:
    f = fixture()
    origin = R.origins(f,'BTCUSD')[0][0]
    prepared = R.REF.prepare(f)
    immediate = R.arm(origin,('immediate','both','wide','short'),f,prepared,'BTCUSD')
    delayed = R.arm(origin,('higher_low3','both','wide','short'),f,prepared,'BTCUSD')
    assert immediate['status'] == delayed['status'] == 'armed'
    assert pd.Timestamp(delayed['decision_close'])-pd.Timestamp(immediate['decision_close']) == pd.Timedelta(minutes=15)
    assert immediate['envelope']['decision_id'] != delayed['envelope']['decision_id']
    assert len(delayed['pivot_proof']) == 2
    assert immediate['pivot_proof'] == []


def test_delay_missing_future_is_censored() -> None:
    f = fixture().iloc[:36]
    origin = R.origins(f,'BTCUSD')[0][0]
    e = R.arm(origin,('delay3','none','wide','short'),f,R.REF.prepare(f),'BTCUSD')
    assert e['status'] == 'censored_missing_decision'


def test_stop_during_wait_rejects_even_if_price_recovers() -> None:
    f = fixture()
    origin = R.origins(f,'BTCUSD')[0][0]
    p = R.REF.prepare(f)
    p.loc[p.index[36],'low'] = origin['stop_price']-1
    assert R.arm(origin,('delay3','none','wide','short'),f,p,'BTCUSD')['status'] == 'rejected_stop_during_delay'


def test_gap_invalidates_session_not_zero_filled() -> None:
    f = fixture()
    f.loc[f.index[5],'eligible'] = False
    assert R.origins(f,'BTCUSD')[0] == []


def test_actual_entry_extension_rejected() -> None:
    at = R.START
    e = {'status':'armed','decision_close':at.isoformat(),'stop_price':95.,
         'target_price':110.,'entry_vwap':100.,'atr':1.,'hold_minutes':15}
    minutes = pd.DataFrame([{'open':102.,'eligible':True}],index=[at])
    assert R.simulate(e,minutes)['status'] == 'rejected_entry_extension'


def test_hold_clock_stop_tie_and_funding_not_fabricated() -> None:
    at = R.START
    e = {'status':'armed','decision_close':at.isoformat(),'stop_price':99.,'target_price':102.,
         'entry_vwap':99.9,'atr':1.,'hold_minutes':15,'net_bps':None,'funding':'unavailable'}
    minutes = pd.DataFrame([{'open':100.,'high':103.,'low':98.,'close':100.,'eligible':True}],index=[at])
    result = R.simulate(e,minutes)
    assert result['exit_reason'] == 'stop_tie'
    assert result['net_bps'] is None
    assert result['net_before_funding_bps'] < result['gross_bps']


def test_sparse_uncertainty_suppressed() -> None:
    e = {'status':'measured','decision_open':R.START.isoformat(),'gross_bps':1.,
         'net_before_funding_bps':-17.,'stress_before_funding_bps':-23.}
    result = R.summarize([e])
    assert result['descriptive_day_bootstrap_95_bps'] is None
    assert result['uncertainty_status'] == 'insufficient_trades_or_days'
