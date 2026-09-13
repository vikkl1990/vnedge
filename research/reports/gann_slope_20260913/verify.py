"""Independent saved-event audit; does not rerun detectors or choose parameters."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify():
    path = HERE/'attempt_01/results.json'
    r = json.loads(path.read_text())
    for p, h in r['source_hashes'].items():
        assert digest(p) == h, p
    assert not any(r[k] for k in ('can_trade', 'can_promote', 'performance_eligible', 'untouched_oos'))
    checks = {}
    for symbol, d in r['symbols'].items():
        for file in d['decision_inputs']['files'] + d['minute_inputs']['files']:
            assert digest(file['path']) == file['sha256']
        bars = pd.concat([pd.read_parquet(f['path']) for f in d['decision_inputs']['files']])
        bars = bars.set_index('open_time').sort_index()
        minutes = pd.concat([pd.read_parquet(f['path']) for f in d['minute_inputs']['files']])
        minutes = minutes.set_index('open_time').sort_index()
        for name, cell in d['cells'].items():
            last_exit, episodes, net = None, set(), []
            counts = cell['counts']
            assert counts['raw_events'] == sum(counts.get(k, 0) for k in
                                               ('measured', 'overlap_skipped', 'censored'))
            for e in cell['events']:
                ts = pd.Timestamp(e['decision_open'])
                close = pd.Timestamp(e['decision_close'])
                entry, exit_at = pd.Timestamp(e['entry_time']), pd.Timestamp(e['exit_time'])
                assert close-ts == pd.Timedelta(minutes=5)
                assert entry-close == pd.Timedelta(minutes=1)
                assert exit_at-entry == pd.Timedelta(minutes=15)
                assert last_exit is None or close >= last_exit
                last_exit = exit_at
                row = bars.loc[ts]
                assert row.content_sha256 == e['decision_hash']
                assert e['side']*(float(row.close)-float(row.open)) > 0
                previous = bars.reindex(pd.date_range(ts-pd.Timedelta(minutes=100), ts,
                                                      freq='5min', inclusive='left'))
                assert previous.content_sha256.notna().all()
                ratio = float(row.quote_volume)/previous.quote_volume.astype(float).median()
                assert ratio >= 1.5 and np.isclose(ratio, e['volume_ratio'])
                if name == 'slope_cross':
                    a = e['anchor']
                    anchor, confirm = pd.Timestamp(a['anchor_open']), pd.Timestamp(a['confirmed_at'])
                    assert confirm-anchor == pd.Timedelta(minutes=20)
                    assert pd.Timedelta(minutes=5) <= close-confirm <= pd.Timedelta(minutes=100)
                    assert bars.loc[anchor].content_sha256 == a['anchor_hash']
                    assert bars.loc[confirm-pd.Timedelta(minutes=5)].content_sha256 == a['confirmation_hash']
                    expected_id = hashlib.sha256(json.dumps(
                        (r['spec'], symbol, a['kind'], a['anchor_hash'], a['confirmation_hash']),
                        sort_keys=True).encode()).hexdigest()
                    assert a['episode_id'] == expected_id and expected_id not in episodes
                    episodes.add(expected_id)
                    swing = bars.reindex(pd.date_range(anchor-pd.Timedelta(minutes=15),
                                                      anchor+pd.Timedelta(minutes=15), freq='5min'))
                    col = 'high' if e['side'] == 1 else 'low'
                    prices = swing[col].astype(float)
                    assert np.isclose(a['price'], prices.iloc[3])
                    others = prices.drop(anchor)
                    assert (others < a['price']).all() if col == 'high' else (others > a['price']).all()
                    warm = bars.reindex(pd.date_range(confirm-pd.Timedelta(minutes=105),
                                                     confirm, freq='5min', inclusive='left'))
                    assert warm.content_sha256.notna().all()
                    h, lo, c = (warm[k].astype(float) for k in ('high', 'low', 'close'))
                    true_range = pd.concat([h-lo, (h-c.shift()).abs(), (lo-c.shift()).abs()], axis=1).max(axis=1)
                    assert np.isclose(true_range.iloc[-20:].mean()/20, a['slope'])
                    elapsed = (close-anchor)/pd.Timedelta(minutes=5)
                    line = a['price'] - e['side']*a['slope']*elapsed
                    prev_line = line+e['side']*a['slope']
                    assert np.isclose(line, e['line']) and np.isclose(prev_line, e['previous_line'])
                    assert e['side']*(float(bars.loc[ts-pd.Timedelta(minutes=5)].close)-prev_line) <= 0
                    assert e['side']*(float(row.close)-line) > 0
                if e['status'] == 'measured':
                    assert float(minutes.loc[entry].open) == e['entry_proxy']
                    assert float(minutes.loc[exit_at].open) == e['exit_proxy']
                    gross = e['side']*(e['exit_proxy']/e['entry_proxy']-1)*10000
                    assert np.isclose(gross, e['gross_bps'])
                    assert np.isclose(gross-17.8, e['net_bps'])
                    net.append(gross-17.8)
            assert len(net) == cell['summary']['n'] == counts.get('measured', 0)
            if net:
                assert np.isclose(np.mean(net), cell['summary']['net_mean_bps'])
            checks[f'{symbol}:{name}'] = dict(measured=len(net), saved_events=len(cell['events']))
    return dict(passed=True, result_sha256=digest(path), checks=checks)


if __name__ == '__main__':
    report = verify()
    with (HERE/'attempt_01/verification.json').open('x') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report, indent=2))
