import zipfile

import numpy as np
import pandas as pd
import pytest

from vnedge.research.archive_scanner_screen import aggregate_archive, features, five_minutes, metrics, replay


def bars(n=120):
    index = pd.date_range('2026-01-01', periods=n, freq='5min', tz='UTC')
    c = 100 + np.sin(np.arange(n)/3) + np.arange(n)/10
    return pd.DataFrame(dict(open=c-.1, high=c+.5, low=c-.5, close=c,
                             volume=np.ones(n), eligible=True), index=index)


def test_chunk_boundary_preserves_rows_and_ohlcv(tmp_path):
    path = tmp_path/'input.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('trades.csv', 'product_symbol,price,size,timestamp,buyer_role\n'
                   'BTCUSD,100,2,2026-01-01 00:00:00,taker\n'
                   'BTCUSD,103,3,2026-01-01 00:00:01,maker\n'
                   'BTCUSD,103,3,2026-01-01 00:00:01,maker\n'
                   'BTCUSD,99,1,2026-01-01 00:01:00,maker\n')
    a, audit = aggregate_archive(path, 'BTCUSD', 1)
    b, _ = aggregate_archive(path, 'BTCUSD', 100)
    pd.testing.assert_frame_equal(a, b)
    assert audit['rows'] == 4
    assert a.iloc[0].volume == pytest.approx(.008)
    assert a.iloc[0].open == 100 and a.iloc[0].close == 103
    assert a.iloc[0].notional == pytest.approx(.818)
    with pytest.raises(ValueError, match='symbol_mismatch'):
        aggregate_archive(path, 'ETHUSD')


def test_causal_prefix_and_reset():
    b = bars()
    full = features(b)
    pd.testing.assert_frame_equal(full.iloc[:80], features(b.iloc[:80]))
    b.loc[b.index[60], 'eligible'] = False
    f = features(b)
    assert not f.warm.iloc[61:110].any()
    assert f.warm.iloc[110]
    assert f.ema50.iloc[109] != f.ema50.iloc[109]  # warmup NaN


def test_partial_minutes_not_filled():
    b = bars(10).drop(columns='eligible')
    b.index = pd.date_range('2026-01-01', periods=10, freq='min', tz='UTC')
    f = five_minutes(b.drop(b.index[2]))
    assert f.eligible.tolist() == [False, True]


def test_next_open_overlap_gap_and_split():
    b = features(bars(20))
    b['pullback'] = 1
    b['regime'] = 1
    e, c = replay(b, 'pullback', 'baseline', '2026-01-01', '2026-01-02', set())
    assert e.iloc[0].gross_bps == pytest.approx((b.open.iloc[4]/b.open.iloc[1]-1)*10000)
    assert c['overlap'] > 0 and c['censored'] > 0
    b.loc[b.index[2], 'eligible'] = False
    e, c = replay(b, 'pullback', 'baseline', '2026-01-01', '2026-01-02', set())
    assert c['censored'] >= 2
    assert e.iloc[0].time != '2026-01-01T00:05:00+00:00'
    e, _ = replay(b, 'pullback', 'session', '2026-01-01', '2026-01-02', set())
    assert e.empty


def test_cost_once_and_drawdown_includes_origin():
    e = pd.DataFrame(dict(time=['2026-01-01T00:00:00Z'], gross_bps=[10.]))
    m = metrics(e)
    assert m['17.8']['mean_net_bps'] == pytest.approx(-7.8)
    assert m['17.8']['additive_drawdown_bps'] == pytest.approx(7.8)
    assert metrics(e.iloc[:0]) == {'n': 0}


@pytest.mark.parametrize('value', ['0', '-1', 'NaN', 'inf'])
def test_invalid_prices_refuse(tmp_path, value):
    path = tmp_path/'bad.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('trades.csv', 'product_symbol,price,size,timestamp,buyer_role\n'
                   f'BTCUSD,{value},2,2026-01-01 00:00:00,taker\n')
    with pytest.raises(ValueError, match='invalid_trade'):
        aggregate_archive(path, 'BTCUSD', 1)


def test_out_of_order_across_chunks_refuses(tmp_path):
    path = tmp_path/'bad.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('trades.csv', 'product_symbol,price,size,timestamp,buyer_role\n'
                   'BTCUSD,100,2,2026-01-01 00:01:00,taker\n'
                   'BTCUSD,100,2,2026-01-01 00:00:00,taker\n')
    with pytest.raises(ValueError, match='unordered_trades'):
        aggregate_archive(path, 'BTCUSD', 1)


def test_split_boundary_censors_outcome():
    b = features(bars(10))
    b.index = pd.date_range('2026-05-31T23:40Z', periods=10, freq='5min')
    b['pullback'] = 0
    b.loc[b.index[0], 'pullback'] = 1
    e, c = replay(b, 'pullback', 'baseline', '2026-01-01', '2026-06-01', set())
    assert e.empty and c['censored'] == 1


def test_regime_filter_is_not_session_filter():
    b = features(bars(10))
    b['pullback'] = 1
    b['regime'] = -1
    e, c = replay(b, 'pullback', 'regime', '2026-01-01', '2026-01-02', {(1, 0)})
    assert e.empty and c['filtered'] == 10
    e, _ = replay(b, 'pullback', 'session', '2026-01-01', '2026-01-02', {(1, 0)})
    assert not e.empty
