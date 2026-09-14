import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location('context_screen_test', Path(__file__).with_name('screen.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def bars(n=350):
    i = np.arange(n)
    close = 100 + .01*i + np.sin(i/3)*.8
    op = np.r_[close[0], close[:-1]]
    return pd.DataFrame(dict(open=op, high=np.maximum(op, close)+.2+.01*np.cos(i), low=np.minimum(op, close)-.2-.01*np.sin(i),
        close=close, volume=np.ones(n), quote_volume=np.where(i%3 == 0, 400., 100.),
        trade_count=np.ones(n, dtype=int), is_closed=True, eligible=True,
        content_sha256=[str(x).zfill(64) for x in i]),
        index=pd.date_range(m.START, periods=n, freq='5min'))


def test_wilder_seed_flat_and_one_way():
    s = pd.Series([100.]*20)
    assert m.wilder(s).iloc[:14].isna().all()
    assert m.wilder(s).iloc[14:].eq(50).all()
    assert m.wilder(pd.Series(np.arange(30.))).iloc[14:].eq(100).all()
    assert m.wilder(pd.Series(-np.arange(30.))).iloc[14:].eq(0).all()


def test_ema_parity_and_gap_reset():
    b = bars()
    out = m.indicators(b)
    expected = b.close.ewm(span=20, adjust=False, min_periods=20).mean()
    pd.testing.assert_series_equal(out.ema20, expected, check_names=False)
    b.loc[b.index[80], 'eligible'] = False
    out = m.indicators(b)
    assert out.ema20.iloc[80:100].isna().all()
    assert not out.warm.iloc[80:130].any()
    pd.testing.assert_series_equal(out.ema20.iloc[81:], b.close.iloc[81:].ewm(
        span=20, adjust=False, min_periods=20).mean(), check_names=False)


def test_hourly_only_complete_children_and_no_carry():
    b = bars(400)
    h = m.hourly_context(b)
    assert h.regime.iloc[:20].eq('unknown').all()
    assert h.regime.iloc[20] != 'unknown'
    b.loc[b.index[260], 'eligible'] = False
    h = m.hourly_context(b)
    assert not h.eligible.iloc[21]
    assert h.regime.iloc[21:].eq('unknown').all()
    assert h.regime.iloc[-1] == 'unknown'  # incomplete final hour


def test_prefix_invariance_and_exact_closed_parent():
    b = bars(350)
    original = b.copy(deep=True)
    full, _ = m.generate(b, 'BTCUSD')
    assert full
    assert any(e['anchors']['swing_low'] and e['anchors']['swing_high'] for e in full)
    for end in (70, 140, 253, 288, 325):
        part, _ = m.generate(b.iloc[:end], 'BTCUSD')
        assert part == [e for e in full if pd.Timestamp(e['decision_open']) <= b.index[end-1]]
    pd.testing.assert_frame_equal(b, original)
    for e in full:
        if e['context_open']:
            assert pd.Timestamp(e['context_open']) == pd.Timestamp(e['decision_close']).floor('1h')-pd.Timedelta(hours=1)
            assert len(e['context_prefix_sha256']) == 64
        for anchors in e['anchors'].values():
            for a in anchors:
                assert pd.Timestamp(a['confirmed_at']) <= pd.Timestamp(e['decision_close'])
                assert pd.Timestamp(a['confirmed_at']) == pd.Timestamp(a['open_time'])+4*m.STEP


def test_future_changes_cannot_change_earlier_features():
    b = bars(310)
    a, _ = m.generate(b, 'BTCUSD')
    b.loc[b.index[290:], ['open', 'high', 'low', 'close']] *= 2
    z, _ = m.generate(b, 'BTCUSD')
    assert [e for e in a if e['decision_open'] < b.index[290].isoformat()] == [
        e for e in z if e['decision_open'] < b.index[290].isoformat()]


def test_forming_and_compressed_gap_refused():
    b = bars()
    b.loc[b.index[10], 'is_closed'] = False
    with pytest.raises(ValueError, match='forming'):
        m.generate(b, 'BTCUSD')
    with pytest.raises(ValueError, match='calendar_slots'):
        m.generate(b.drop(b.index[11]), 'BTCUSD')


def test_group_caps_missingness_and_permission():
    features = {g: {'a': True} for g in m.GROUPS}
    a, group, coverage = m.scores(features, 'up')
    assert a['balanced60'] == 100
    features['structure']['duplicate'] = True
    assert m.scores(features, 'up')[0]['balanced60'] == 100
    features['structure'] = {'unknown': None}
    a, group, coverage = m.scores(features, 'up')
    assert a['balanced60'] == 80 and coverage['structure'] == 0
    e = dict(regime='unknown', side=1, scores=a)
    assert not m.allowed(e, 'regime60', {})
    e['regime'], e['side'] = 'up', -1
    assert not m.allowed(e, 'regime60', {})


def test_cells_training_only_minimum_days_and_no_fallback():
    base = dict(symbol='BTCUSD', family='breakout20', session='UTC_00_06', regime='up',
                status='measured', net_bps=10., side=1, scores={'regime60': 100})
    rows = [dict(base, decision_close=f'2026-09-0{1+i%3}T02:00:00+00:00',
                 exit_time=f'2026-09-0{1+i%3}T02:16:00+00:00') for i in range(30)]
    assert not m.fit_cells(rows[:29])[m.cell_key(base)]['allowed']
    cells = m.fit_cells(rows)
    assert cells[m.cell_key(base)]['allowed']
    assert m.allowed(base, 'session_regime60', cells)
    assert not m.allowed(dict(base, symbol='ETHUSD'), 'session_regime60', cells)
    leaked = dict(base, decision_close=m.SPLIT.isoformat(), exit_time=m.END.isoformat(), net_bps=1e9)
    assert m.fit_cells(rows+[leaked]) == cells


def test_gap_resets_anchors_and_no_early_events():
    b = bars()
    b.loc[b.index[200], 'eligible'] = False
    events, _ = m.generate(b, 'BTCUSD')
    assert not [e for e in events if b.index[200] <= pd.Timestamp(e['decision_open']) < b.index[250]]
    for e in events:
        if pd.Timestamp(e['decision_open']) >= b.index[250]:
            assert all(a['index'] > 200 for group in e['anchors'].values() for a in group)


def test_measurement_clock_censor_cost_once():
    helper = m.ref('context_test_measure', 'gann_slope_20260913')
    minutes = pd.DataFrame(dict(open=100., eligible=True), index=pd.date_range(m.START, periods=70, freq='min'))
    close = m.START+5*pd.Timedelta(minutes=1)
    e = dict(decision_open=m.START.isoformat(), decision_close=close.isoformat(), side=1)
    minutes.loc[close+pd.Timedelta(minutes=16), 'open'] = 101.
    rows, _ = helper.measure([e], minutes)
    assert rows[0]['entry_time'] == (close+pd.Timedelta(minutes=1)).isoformat()
    assert rows[0]['net_bps'] == pytest.approx(82.2)
    minutes.loc[close+pd.Timedelta(minutes=3), 'eligible'] = False
    rows, counts = helper.measure([e, dict(e, decision_close=(close+m.STEP).isoformat())], minutes)
    assert counts['censored'] == 1 and counts['overlap_skipped'] == 1
