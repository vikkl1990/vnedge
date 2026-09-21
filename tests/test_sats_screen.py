from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from vnedge.research.sats_screen import (
    Settings, Trade, intrabar, plan, rma, settings, signal_frame, simulate, summarize,
)


def bars(n=300):
    c = 100+5*np.sin(np.arange(n)/7)+np.arange(n)*.01
    return pd.DataFrame(dict(open=c-.1, high=c+.4, low=c-.4, close=c,
                             volume=100., eligible=True),
                        index=pd.date_range('2026-08-01', periods=n, freq='5min', tz='UTC'))


def trade(side=1, fee=0):
    return Trade(side, 100., 100.-side*10, [100.+side*10*r for r in (1, 2, 3)],
                 10., 0, '2026-08-01T00:00:00Z', .5, fee, fees=100*fee/10000)


def test_atr_sma_seed_then_wilder():
    x = rma(np.array([1., 2., 3., 10.]), 3)
    assert np.isnan(x[:2]).all()
    assert x[2] == 2 and x[3] == pytest.approx(14/3)


def test_presets():
    assert settings('BTCUSD', 5, 'Auto') == Settings(10, 1.5, 14, 1., .5)
    assert settings('ETHUSD', 15, 'Crypto 24/7') == Settings(14, 2.8, 20, 2.5, .05)
    with pytest.raises(ValueError):
        settings('BTCUSD', 60, 'Auto')


def test_causal_prefix_and_gap_reset():
    b = bars()
    cfg = settings('ETHUSD', 5, 'Auto')
    f = signal_frame(b, cfg)
    pd.testing.assert_frame_equal(f.iloc[:200], signal_frame(b.iloc[:200], cfg))
    assert f.signal.ne(0).any()
    assert f.signal.iloc[:108].eq(0).all()
    b.loc[b.index[140], 'eligible'] = False
    f = signal_frame(b, cfg)
    assert f.signal.iloc[141:249].eq(0).all()
    with pytest.raises(ValueError, match='calendar'):
        signal_frame(b.drop(b.index[50]), cfg)


def test_pivot_not_available_until_three_bars_later():
    b = bars(10)
    b['low'] = [90, 89, 88, 80, 89, 90, 91, 92, 93, 94]
    f = signal_frame(b, settings('ETHUSD', 5, 'Auto'))
    assert f.pivot_low.iloc[:6].isna().all()
    assert f.pivot_low.iloc[6] == 80


@pytest.mark.parametrize('side', [1, -1])
def test_stop_wins_ambiguous_candle(side):
    t = trade(side)
    reason = intrabar(t, SimpleNamespace(open=100, high=140, low=60), 0)
    assert reason == 'SL'
    assert t.pnl/t.risk == -1 and not any(t.hits)


def test_stop_gap_not_clipped():
    t = trade()
    intrabar(t, SimpleNamespace(open=70, high=80, low=60), 3)
    assert t.pnl/t.risk < -3


@pytest.mark.parametrize('side', [1, -1])
def test_partial_targets_initial_r_and_fees(side):
    t = trade(side, 5.9)
    r = SimpleNamespace(open=100, high=135 if side == 1 else 101,
                        low=99 if side == 1 else 65)
    assert intrabar(t, r, 3) == 'TP3'
    assert t.remaining == pytest.approx(0)
    assert t.pnl/t.risk == pytest.approx(2)
    assert t.fees == pytest.approx((100+(110+120+130)/3 if side == 1 else
                                   100+(90+80+70)/3)*5.9/10000)


def test_plan_cap_and_no_pivot_fallback():
    row = SimpleNamespace(signal=1, close=100, low=99, high=101, atr=2,
                          pivot_low=50, pivot_high=np.nan, tqi=.5)
    cfg = Settings(10, 1.5, 14, 1, .5)
    p = plan(row, cfg)
    assert p['stop'] == 92 and p['targets'] == [108, 116, 124]
    row.pivot_low = np.nan
    assert plan(row, cfg)['stop'] == 97


def execution_frame():
    b = bars(5)
    b['open'], b['close'], b['high'], b['low'] = 100., 100., 100., 99.5
    b['signal'], b['trend'], b['atr'], b['tqi'] = 0, 1, 2., .5
    b['pivot_low'], b['pivot_high'] = np.nan, np.nan
    b.loc[b.index[0], 'signal'] = 1
    return b


def test_next_open_entry_manages_its_first_bar():
    b = execution_frame()
    b.loc[b.index[1], 'low'] = 90
    records, counts = simulate(b, settings('ETHUSD', 5, 'Auto'), 'next_open_gross',
                               '2026-08-01', '2026-09-01')
    assert counts['entries'] == 1 and records[0]['reason'] == 'SL'
    assert records[0]['time'] == b.index[1].isoformat()


def test_signal_candle_extremes_not_used_after_close_entry():
    b = execution_frame()
    b.loc[b.index[0], ['low', 'high']] = [10, 200]
    records, _ = simulate(b, settings('ETHUSD', 5, 'Auto'), 'source_close_gross',
                           '2026-08-01', '2026-09-01')
    assert records[0]['reason'] == 'CENSORED_END'
    assert summarize(records) == {'closed': 0}


def test_gap_censors_existing_trade_and_cancels_pending():
    b = execution_frame()
    b.loc[b.index[1], 'eligible'] = False
    records, counts = simulate(b, settings('ETHUSD', 5, 'Auto'), 'next_open_gross',
                               '2026-08-01', '2026-09-01')
    assert records == [] and counts['pending_gap'] == 1
    records, counts = simulate(b, settings('ETHUSD', 5, 'Auto'), 'source_close_gross',
                               '2026-08-01', '2026-09-01')
    assert records[0]['reason'] == 'CENSORED_GAP' and counts['censored'] == 1


def test_gap_past_first_target_refuses_delayed_entry():
    b = execution_frame()
    b.loc[b.index[1], 'open'] = 150
    records, counts = simulate(b, settings('ETHUSD', 5, 'Auto'), 'next_open_gross',
                               '2026-08-01', '2026-09-01')
    assert records == [] and counts['rejected_entry'] == 1


def test_pending_flip_exits_before_reverse_entry():
    b = execution_frame()
    b.loc[b.index[2]:, 'trend'] = -1
    b.loc[b.index[2], 'signal'] = -1
    records, counts = simulate(b, settings('ETHUSD', 5, 'Auto'), 'next_open_cost',
                               '2026-08-01', '2026-09-01')
    assert counts['entries'] == 2
    assert records[0]['reason'] == 'FLIP' and records[0]['remaining'] == 0
    assert records[1]['side'] == -1 and records[1]['reason'] == 'CENSORED_END'
