"""Exploratory release regression; no orders, tuning, or promotion.

Fixed before observing results: all available Delta canonical bars through
2026-09-08 18:15 UTC, and 12:00-16:00 UTC on the last complete captured quote
day (2026-09-04). The quote interval covers the registered session window.
"""
from __future__ import annotations

import gc
import hashlib
import json
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from vnedge.research.scanner_evidence import (
    atomic_write, load_evidence_frame, normalize_canonical_candles,
    replay_quote_scanner, replay_scanner,
)
from vnedge.runtime.multi_lane import _overlay_canonical_history, _canonical_candle_frame
from vnedge.data.candles import CandleParquetStore

OUT = Path('/output')
DATA = Path('/evidence/data')
LOGS = Path('/evidence/logs/paper_trials')
CUTOFF = pd.Timestamp('2026-09-08T18:15:00Z')
QSTART = pd.Timestamp('2026-09-04T12:00:00Z')
QEND = pd.Timestamp('2026-09-04T16:00:00Z')
SIZES = {'5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
summary = {'revision': '86e8a65', 'classification': 'exploratory_release_regression',
           'can_trade': False, 'can_promote': False, 'approval_parity': False,
           'bar_cutoff': str(CUTOFF), 'quote_window': [str(QSTART), str(QEND)],
           'runs': [], 'limitations': [
               'No funding or shared-account risk replay; no promotion-grade PnL.',
               'Old captured runtime cannot prove parity with a corrected acceptance epoch.',
               'No lane-consumed Delta squeeze_v4 or HTF quote-continuation tape found.',
               'Bar studies are next-open mechanism economics, not kernel execution.',
           ]}


def bars(symbol: str, tf: str, cutoff: pd.Timestamp = CUTOFF) -> pd.DataFrame:
    # Use production's persisted-identity/coverage filter, not raw legacy
    # Parquet nulls. Missing records stay missing; do not fabricate hashes.
    frame = _canonical_candle_frame(CandleParquetStore(DATA / 'candles', exchange='delta_india'),
        symbol, tf, since_ms=0, until_ms=int(cutoff.timestamp()*1000))
    return frame.loc[frame.timestamp + pd.Timedelta(seconds=SIZES[tf]) <= cutoff].reset_index(drop=True)


def context(symbol: str, tf: str) -> pd.DataFrame:
    paths = list(LOGS.glob(f'*__{symbol.lower()}_delta_india_*.context_{tf}.candles.parquet'))
    if len(paths) != 1:
        raise ValueError(f'expected one frozen runtime context cache: {symbol}/{tf}: {paths}')
    exact = bars(symbol, tf)
    official = load_evidence_frame(paths[0])
    # Same runtime binding rule; official prices do not acquire exact volume.
    return _overlay_canonical_history(official, exact,
        allow_validated_exchange_ohlcv=True, timeframe=tf, symbol=symbol)


def record(name: str, result: dict, provenance: dict) -> None:
    result['release_revision'] = '86e8a65'
    result['run_provenance'] = provenance
    result['approval_parity_eligible'] = False
    result['performance_eligible'] = False
    atomic_write(OUT / f'{name}.json', result)
    keys = ('strategy_id', 'symbol', 'entry_clock', 'cost_profile', 'execution_cost_bps',
            'bars', 'evaluations', 'signals', 'trades', 'intents', 'outcomes',
            'net_execution_bps', 'gross_bps', 'failed_gates', 'quotes_used',
            'capture_quality', 'setup_funnel', 'context_quality', 'engine')
    compact = {k: result[k] for k in keys if k in result}
    compact.update(name=name, provenance=provenance)
    summary['runs'].append(compact)
    atomic_write(OUT / 'summary.json', summary)
    print(json.dumps({'finished': name, **{k: compact.get(k) for k in (
        'bars', 'signals', 'trades', 'intents', 'outcomes', 'net_execution_bps', 'quotes_used')}}), flush=True)


for symbol in ('BTCUSD', 'ETHUSD'):
    for sid, tf in ((f'htf_regime_continuation_15m_v2__{symbol}', '15m'),
                    ('range_expansion_observer_v4', '15m'),
                    ('trend_squeeze_continuation_1h_v1', '1h')):
        name = f'bar_{symbol}_{sid}'
        print('starting', name, flush=True)
        try:
            frame = bars(symbol, tf)
            contexts = {t: context(symbol, t) for t in ('4h', '1d')} if sid.startswith('htf_regime') else None
            result = replay_scanner(sid, frame, exchange_id='delta_india', context_candles=contexts)
            result['symbol'] = symbol
            record(name, result, {'start': str(frame.timestamp.min()), 'last_open': str(frame.timestamp.max()),
                'bar_identity_digest': hashlib.sha256('\n'.join(frame.content_sha256).encode()).hexdigest(),
                'source_counts': frame.candle_source.value_counts().to_dict()})
        except Exception as exc:
            summary['runs'].append({'name': name, 'error': f'{type(exc).__name__}: {exc}'})
            atomic_write(OUT / 'summary.json', summary)
            print('error', name, repr(exc), flush=True)
        gc.collect()

for symbol in ('BTCUSD', 'ETHUSD'):
    for sid in ('range_expansion_realtime_v2', 'session_continuation_realtime_v2', 'structure_bos_realtime_v2'):
        name = f'quote_{symbol}_{sid}'
        print('starting', name, flush=True)
        try:
            lanes = list((DATA / 'quote_evidence').glob(f'lane=*{sid}_delta_india_{symbol[:3].lower()}_usd_usd_15m'))
            if len(lanes) != 1:
                raise ValueError(f'missing/ambiguous lane evidence: {lanes}')
            paths = sorted(lanes[0].glob(f'**/{QSTART.strftime("%Y%m%d")}/*.parquet'))
            chunks, consumed = [], []
            for path in paths:
                pf = pq.ParquetFile(path)
                # Prune by venue timestamp metadata, never by price or result.
                col = pf.schema_arrow.names.index('ts_ms')
                stats = [pf.metadata.row_group(i).column(col).statistics for i in range(pf.num_row_groups)]
                if stats and all(s is not None and s.has_min_max for s in stats):
                    if max(s.max for s in stats) < QSTART.timestamp()*1000 or min(s.min for s in stats) >= QEND.timestamp()*1000:
                        continue
                for batch in pf.iter_batches(batch_size=10000):
                    f = batch.to_pandas()
                    f = f.loc[(f.ts_ms >= QSTART.timestamp()*1000) & (f.ts_ms < QEND.timestamp()*1000)]
                    if not f.empty:
                        chunks.append(f)
                        consumed.append(str(path))
            if not chunks:
                raise ValueError('no captured quotes in fixed window')
            quotes = pd.concat(chunks, ignore_index=True)
            del chunks
            print('quotes_loaded', name, len(quotes), flush=True)
            frame = bars(symbol, '15m', QEND)
            contexts = {'4h': bars(symbol, '4h', QEND)} if sid.startswith('structure_bos') else None
            result = replay_quote_scanner(sid, frame, quotes, symbol=symbol,
                exchange_id='delta_india', context_candles=contexts,
                evidence_start=QSTART.to_pydatetime(), evidence_end=QEND.to_pydatetime(),
                approval_parity_eligible=False, approval_mode='research_only_no_shared_gateway')
            record(name, result, {'quote_shards': consumed, 'quote_rows': len(quotes),
                'quote_frame_digest': hashlib.sha256(pd.util.hash_pandas_object(quotes, index=False).values.tobytes()).hexdigest(),
                'entry_assumption': 'lane_BBO_mechanism_only', 'window': [str(QSTART), str(QEND)]})
            del quotes, result
        except Exception as exc:
            summary['runs'].append({'name': name, 'error': f'{type(exc).__name__}: {exc}'})
            atomic_write(OUT / 'summary.json', summary)
            print('error', name, repr(exc), flush=True)
        gc.collect()
summary['complete'] = True
atomic_write(OUT / 'summary.json', summary)
