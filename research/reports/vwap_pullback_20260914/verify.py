"""Independent evidence and arithmetic verification; no new parameter trial."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify() -> None:
    root = HERE/'attempt_01'
    result = json.loads((root/'results.json').read_text())
    for path,expected in result['source_hashes'].items():
        assert sha(Path(path)) == expected
    assert result['trial_cells'] == 96
    report = {'verified':True,'result_sha256':sha(root/'results.json'),
              'verifier_sha256':sha(Path(__file__)),'can_trade':False,'can_promote':False,'symbols':{}}
    for symbol,data in result['symbols'].items():
        path = root/f'{symbol}.events.json'
        assert sha(path) == data['events_sha256']
        records = json.loads(path.read_text())
        assert len(records) == len(data['cells']) == 48
        input_files = data['inputs']['minutes']['files']+data['inputs']['decisions']
        for item in input_files:
            assert sha(Path(item['path'])) == item['sha256']
        minute = pd.concat([pd.read_parquet(p['path']) for p in data['inputs']['minutes']['files']]).set_index('open_time')
        canonical = records['immediate_none_wide_short']
        common = [e['episode_id'] for e in canonical]
        assert len(common) == len(set(common)) == data['diagnostics']['common_origins']
        ids,unique_fills = set(),set()
        pivot_checks = 0
        for name,events in records.items():
            assert [e['episode_id'] for e in events] == common
            assert len(events) == sum(data['cells'][name]['summary']['counts'].values())
            measured = []
            for e in events:
                assert e['net_bps'] is None and e['can_trade'] is False and e['can_promote'] is False
                if 'envelope' in e:
                    decision = e['envelope']['decision_id']
                    assert decision not in ids
                    ids.add(decision)
                    delay = pd.Timedelta(minutes=0 if name.startswith('immediate_') else 15)
                    assert pd.Timestamp(e['decision_close']) == pd.Timestamp(e['origin_close'])+delay
                for p in e.get('pivot_proof',[]):
                    assert pd.Timestamp(p['confirmed_at']) <= pd.Timestamp(e['decision_close'])
                    assert pd.Timestamp(p['confirmed_at']) == pd.Timestamp(p['anchor_open'])+pd.Timedelta(minutes=20)
                    pivot_checks += 1
                if e['status'] == 'measured':
                    measured.append(e)
                    unique_fills.add(e['episode_id'])
                    entry,exit_price = e['entry_proxy'],e['exit_proxy']
                    buy,sell = entry*1.0003,exit_price*.9997
                    value=(sell-buy-.00059*(buy+sell))/buy*10000
                    assert abs(value-e['net_before_funding_bps']) < 1e-8
                    assert 0 < buy-e['entry_vwap'] <= e['atr']
            summary = data['cells'][name]['summary']
            assert summary['n'] == len(measured)
            if measured:
                assert abs(summary['net_before_funding_mean_bps']-np.mean([e['net_before_funding_bps'] for e in measured])) < 1e-9
        distances = []
        for e in canonical:
            if 'entry_vwap' in e:
                entry = float(minute.loc[pd.Timestamp(e['decision_close']),'open'])*1.0003
                distances.append({'origin_open':e['origin_open'],
                                  'extension_atr':(entry-e['entry_vwap'])/e['atr'],
                                  'status':e['status']})
        report['symbols'][symbol] = {
            'common_setups':len(common),'unique_measured_episodes':len(unique_fills),
            'nonzero_cells':sum(c['summary']['n']>0 for c in data['cells'].values()),
            'unique_variant_arm_ids':len(ids),'pivot_timestamps_checked':pivot_checks,
            'immediate_entry_diagnostics':distances,
            'contraction_setups':sum(e['contraction_ok'] for e in canonical),
            'expansion_setups':sum(e['expansion_ok'] for e in canonical),
            'both_volume_setups':sum(e['contraction_ok'] and e['expansion_ok'] for e in canonical),
            'baseline_timing_counts':{k:dict(Counter(e['status'] for e in records[k])) for k in
                                      ('immediate_none_wide_short','delay3_none_wide_short','higher_low3_none_wide_short')}}
    with (root/'verification.json').open('x') as stream:
        json.dump(report,stream,indent=2,allow_nan=False)
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    verify()
