"""Independent artifact arithmetic and identity checks; no strategy rerun."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify() -> None:
    output = HERE / 'attempt_01'
    result = json.loads((output / 'results.json').read_text())
    assert result['source_sha256'] == sha(HERE / 'replay.py')
    assert result['contract_sha256'] == sha(HERE / 'CONTRACT.md')
    assert result['can_trade'] is False and result['can_promote'] is False
    checked = 0
    identities: set[str] = set()
    for symbol, data in result['symbols'].items():
        path = output / f'{symbol}.events.json'
        assert sha(path) == data['events_sha256']
        events = json.loads(path.read_text())
        for item in data['inputs']['minutes']['files'] + data['inputs']['decisions']:
            assert sha(Path(item['path'])) == item['sha256']
        previous = None
        for event in events:
            ts = pd.Timestamp(event['decision_close'])
            assert ts == pd.Timestamp(event['decision_open']) + pd.Timedelta(minutes=5)
            if previous is not None:
                assert ts >= previous + pd.Timedelta(minutes=30)
            previous = ts
            assert event['net_bps'] is None and event['funding'] == 'unavailable'
            for envelope in event['envelopes'].values():
                assert envelope['decision_id'] not in identities
                identities.add(envelope['decision_id'])
            if event['status'] == 'measured':
                entry, exit_price = event['entry_proxy'], event['exit_proxy']
                buy, sell = entry * 1.0003, exit_price * .9997
                expected = ((sell-buy)-.00059*(buy+sell))/buy*10000
                assert abs(expected-event['net_before_funding_bps']) < 1e-8
                assert abs((exit_price/entry-1)*10000-event['gross_bps']) < 1e-8
                checked += 1
        for variant, cell in data['cells'].items():
            chosen = [e for e in events if e['status'] == 'measured'
                      and (variant not in ('vwap', 'both') or e['vwap_ok'])
                      and (variant not in ('volume', 'both') or e['volume_ok'])]
            summary = cell['summary']
            assert summary['n'] == len(chosen)
            if chosen:
                values = np.array([e['net_before_funding_bps'] for e in chosen])
                assert abs(summary['net_before_funding_mean_bps']-values.mean()) < 1e-10
                curve = np.r_[0, values.cumsum()]
                assert abs(summary['drawdown_unit_notional_bps']-
                           (np.maximum.accumulate(curve)-curve).max()) < 1e-10
    dependencies = [HERE / 'replay.py', HERE / 'CONTRACT.md', Path(__file__),
                    HERE.parent / 'burst_response_20260912/screen.py',
                    HERE.parent / 'range_retest_20260912/replay.py']
    dependencies += [ROOT / 'src/vnedge' / p for p in (
        'data/bar_identity.py', 'data/vwap.py', 'plan/cost_model.py',
        'plan/cash_costs.py', 'strategy/arm_evidence.py',
        'strategy/base_strategy.py', 'execution/evidence.py')]
    report = {'verified': True, 'measured_baseline_episodes': checked,
              'unique_variant_decision_ids': len(identities),
              'result_sha256': sha(output / 'results.json'),
              'source_hashes': {str(p.relative_to(ROOT)): sha(p) for p in dependencies},
              'can_trade': False, 'can_promote': False,
              'scope': 'Artifact identities and arithmetic, not economic or execution proof'}
    with (output / 'verification.json').open('x') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    verify()
