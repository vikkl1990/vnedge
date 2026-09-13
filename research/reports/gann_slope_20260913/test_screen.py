import importlib.util
from pathlib import Path

import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location('gann_test_screen', Path(__file__).with_name('screen.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture(short=False):
    bars = pd.DataFrame(dict(open=[100.]*34, high=[101.]*34, low=[99.]*34,
                             close=[100.]*34, volume=[1.]*34, quote_volume=[100.]*34,
                             trade_count=[1]*34, is_closed=[True]*34, eligible=[True]*34,
                             content_sha256=[str(i).zfill(64) for i in range(34)]),
                        index=pd.date_range(m.START, periods=34, freq='5min'))
    bars.iloc[24, bars.columns.get_loc('high')] = 110.
    for i, op, hi, cl in ((28, 100., 111., 110.), (29, 110., 112., 111.),
                          (30, 100., 101., 100.), (31, 100., 112., 111.)):
        bars.iloc[i, bars.columns.get_indexer(['open', 'high', 'close', 'quote_volume'])] = (
            op, hi, cl, 300.)
    if short:
        original = bars.copy()
        bars['open'], bars['close'] = 200-original.open, 200-original.close
        bars['high'], bars['low'] = 200-original.low, 200-original.high
    return bars


@pytest.mark.parametrize('short', [False, True])
def test_confirmed_anchor_cross_is_exercised_and_episode_not_repeated(short):
    rows, _, counts = m.generate(fixture(short), 'BTCUSD')
    assert len(rows) == 1
    event = rows[0]
    assert event['side'] == (-1 if short else 1)
    assert event['decision_open'] == (m.START+28*m.STEP).isoformat()
    assert event['anchor']['anchor_open'] == (m.START+24*m.STEP).isoformat()
    assert event['anchor']['confirmed_at'] == (m.START+28*m.STEP).isoformat()
    assert pd.Timestamp(event['decision_close']) > pd.Timestamp(event['anchor']['confirmed_at'])
    assert counts['episode_already_emitted'] == 1


def test_prefix_invariance_and_no_mutation():
    bars = fixture()
    original = bars.copy(deep=True)
    expected, _, _ = m.generate(bars, 'BTCUSD')
    assert expected
    for end in range(21, len(bars)+1):
        actual, _, _ = m.generate(bars.iloc[:end], 'BTCUSD')
        cutoff = bars.index[end-1]+m.STEP
        assert actual == [e for e in expected if pd.Timestamp(e['decision_close']) <= cutoff]
    pd.testing.assert_frame_equal(bars, original)


def test_gap_cannot_bridge_into_anchor_or_signal():
    bars = fixture()
    bars.iloc[25, bars.columns.get_loc('eligible')] = False
    assert not m.generate(bars, 'BTCUSD')[0]


def test_forming_window_is_refused():
    bars = fixture()
    bars.iloc[24, bars.columns.get_loc('is_closed')] = False
    with pytest.raises(ValueError, match='closed candles'):
        m.generate(bars, 'BTCUSD')


def test_volume_is_prior_only_and_required():
    bars = fixture().iloc[:29].copy()
    bars.iloc[28, bars.columns.get_loc('quote_volume')] = 149.
    assert not m.generate(bars, 'BTCUSD')[0]
    bars.iloc[28, bars.columns.get_loc('quote_volume')] = 150.
    assert len(m.generate(bars, 'BTCUSD')[0]) == 1


def minute_fixture():
    return pd.DataFrame(dict(open=[100.]*80, eligible=[True]*80),
                        index=pd.date_range(m.START, periods=80, freq='min'))


def event(close):
    return dict(decision_open=(close-m.STEP).isoformat(), decision_close=close.isoformat(), side=1)


def test_delay_horizon_and_cost_once():
    minutes = minute_fixture()
    close = m.START+pd.Timedelta(minutes=5)
    minutes.loc[close+pd.Timedelta(minutes=16), 'open'] = 101.
    rows, counts = m.measure([event(close)], minutes)
    assert counts['measured'] == 1
    assert rows[0]['entry_time'] == (close+pd.Timedelta(minutes=1)).isoformat()
    assert rows[0]['gross_bps'] == pytest.approx(100.)
    assert rows[0]['net_bps'] == pytest.approx(82.2)


def test_censor_reserves_interval_and_next_at_exit_is_allowed():
    minutes = minute_fixture()
    close = m.START+pd.Timedelta(minutes=5)
    minutes.loc[close+pd.Timedelta(minutes=4), 'eligible'] = False
    rows, counts = m.measure([event(close), event(close+pd.Timedelta(minutes=5)),
                             event(close+pd.Timedelta(minutes=16))], minutes)
    assert counts == dict(raw_events=3, censored=1, overlap_skipped=1, measured=1)
    assert len(rows) == 2


def test_result_directory_cannot_be_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        m.run(tmp_path, tmp_path)
