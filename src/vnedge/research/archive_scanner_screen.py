"""Bounded archive hypothesis screen; deliberately has no execution imports."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

FAMILIES = ('pullback', 'reclaim', 'compression')
VARIANTS = ('baseline', 'regime', 'session', 'both')
PERIODS = {'train': ('2026-01-01', '2026-06-01'),
           'validation': ('2026-06-01', '2026-08-01'),
           'holdout': ('2026-08-01', '2026-09-01'),
           'diagnostic': ('2026-09-01', '2026-09-02')}
STEP = pd.Timedelta(minutes=5)


def sha(path: Path) -> str:
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def aggregate_archive(path: Path, symbol: str, chunksize: int = 250_000
                      ) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Combine ordered partial minute aggregates without dropping equal rows."""
    parts, count, previous = [], 0, None
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != 1 or not names[0].endswith('.csv'):
            raise ValueError('single_csv_required')
        with z.open(names[0]) as f:
            for chunk in pd.read_csv(f, chunksize=chunksize):
                if not chunk.product_symbol.eq(symbol).all():
                    raise ValueError('symbol_mismatch')
                ts = pd.to_datetime(chunk.timestamp, format='ISO8601', utc=True)
                values = chunk[['price', 'size']].to_numpy(dtype=float)
                if (ts.isna().any() or not np.isfinite(values).all() or
                        (values <= 0).any() or (chunk['size'] % 1 != 0).any() or
                        not chunk.buyer_role.isin(['maker', 'taker']).all()):
                    raise ValueError('invalid_trade')
                if not ts.is_monotonic_increasing or (previous is not None and ts.iloc[0] < previous):
                    raise ValueError('unordered_trades')
                previous = ts.iloc[-1]
                chunk['time'] = ts.dt.floor('min')
                chunk['volume'] = chunk['size'] * {'BTCUSD': .001, 'ETHUSD': .01}[symbol]
                chunk['notional'] = chunk.price * chunk.volume
                parts.append(chunk.groupby('time', sort=True).agg(
                    open=('price', 'first'), high=('price', 'max'), low=('price', 'min'),
                    close=('price', 'last'), volume=('volume', 'sum'),
                    notional=('notional', 'sum'), trades=('price', 'size')))
                count += len(chunk)
    if not parts:
        raise ValueError('empty_archive')
    bars = pd.concat(parts).groupby(level=0, sort=True).agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last',
         'volume': 'sum', 'notional': 'sum', 'trades': 'sum'})
    return bars, dict(path=str(path), sha256=sha(path), rows=count,
                      first_minute=bars.index[0].isoformat(), last_minute=bars.index[-1].isoformat())


def five_minutes(minutes: pd.DataFrame) -> pd.DataFrame:
    if not minutes.index.is_unique or not minutes.index.is_monotonic_increasing:
        raise ValueError('minute_identity')
    groups = minutes.resample('5min')
    b = groups.agg({'open': 'first', 'high': 'max', 'low': 'min',
                    'close': 'last', 'volume': 'sum'})
    b['eligible'] = groups.size().eq(5)
    return b


def features(bars: pd.DataFrame) -> pd.DataFrame:
    b = bars.copy()
    b['segment'] = (~b.eligible).cumsum()
    for name in ('ema20', 'ema50', 'atr', 'er', 'hi', 'lo', 'volbase', 'prior_atr',
                 'prior_close', 'prior_ema'):
        b[name] = np.nan
    b['warm'] = False
    for _, p in b[b.eligible].groupby('segment'):
        c = p.close
        ema = c.ewm(span=20, adjust=False, min_periods=20).mean()
        tr = pd.concat([p.high-p.low, (p.high-c.shift()).abs(),
                        (p.low-c.shift()).abs()], axis=1).max(axis=1).rolling(20).mean()
        vals = dict(ema20=ema, ema50=c.ewm(span=50, adjust=False, min_periods=50).mean(),
                    atr=tr, er=c.diff(20).abs()/c.diff().abs().rolling(20).sum().replace(0, np.nan),
                    hi=p.high.shift().rolling(20).max(), lo=p.low.shift().rolling(20).min(),
                    volbase=p.volume.shift().rolling(20).median(), prior_atr=tr.shift(),
                    prior_close=c.shift(), prior_ema=ema.shift())
        for key, v in vals.items():
            b.loc[p.index, key] = v
        b.loc[p.index[49:], 'warm'] = True
    b['regime'] = np.select([(b.er >= .3) & (b.ema20 > b.ema50),
                             (b.er >= .3) & (b.ema20 < b.ema50)], [1, -1], default=0)
    for family in FAMILIES:
        sides = []
        for s in (1, -1):
            directional = s*(b.close-b.open) > 0
            if family == 'pullback':
                touch = b.low <= b.ema20 if s == 1 else b.high >= b.ema20
                rule = (s*(b.prior_close-b.prior_ema) > 0) & touch & (
                    s*(b.close-b.ema20) > 0) & (s*(b.close-b.prior_close) > 0) & directional
            elif family == 'reclaim':
                rule = ((b.low < b.lo) & (b.close > b.lo) if s == 1 else
                        (b.high > b.hi) & (b.close < b.hi)) & directional
            else:
                rule = (b.hi-b.lo <= 4*b.prior_atr) & (b.volume >= 1.5*b.volbase) & (
                    b.close > b.hi if s == 1 else b.close < b.lo)
            sides.append(rule & b.warm & b.eligible)
        b[family] = np.select([sides[0] & ~sides[1], sides[1] & ~sides[0]], [1, -1], default=0)
    return b


def replay(b: pd.DataFrame, family: str, variant: str, start: str, end: str,
           sessions: set[tuple[int, int]]) -> tuple[pd.DataFrame, dict[str, int]]:
    start_t, end_t = pd.Timestamp(start, tz='UTC'), pd.Timestamp(end, tz='UTC')
    rows, busy = [], -1
    counts = dict(candidates=0, filtered=0, overlap=0, censored=0)
    for i in np.flatnonzero(b[family].to_numpy()):
        ts = b.index[i]
        if not start_t <= ts + STEP < end_t:
            continue
        r, side = b.iloc[i], int(b.iloc[i][family])
        counts['candidates'] += 1
        block = (ts + STEP).hour // 6
        aligned = r.regime == (0 if family == 'reclaim' else side)
        if (variant in ('regime', 'both') and not aligned) or (
                variant in ('session', 'both') and (side, block) not in sessions):
            counts['filtered'] += 1
            continue
        if i < busy:
            counts['overlap'] += 1
            continue
        busy = i+4
        if (i+4 >= len(b) or b.index[i+4] >= end_t or
                not b.eligible.iloc[i:i+5].all() or
                b.index[i+4]-ts != 4*STEP):
            counts['censored'] += 1
            continue
        entry, exit_price = float(b.open.iloc[i+1]), float(b.open.iloc[i+4])
        rows.append(dict(time=(ts+STEP).isoformat(), side=side, block=block,
                         gross_bps=side*(exit_price/entry-1)*10000))
    return pd.DataFrame(rows, columns=['time', 'side', 'block', 'gross_bps']), counts


def metrics(events: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = dict(n=len(events))
    if events.empty:
        return out
    gross = events.gross_bps.to_numpy()
    out['mean_gross_bps'] = float(gross.mean())
    for cost in (11.8, 17.8, 23.8):
        net = gross-cost
        equity = np.r_[0., np.cumsum(net)]
        losses = -net[net < 0].sum()
        out[str(cost)] = dict(mean_net_bps=float(net.mean()), win_fraction=float((net > 0).mean()),
                             profit_factor=float(net[net > 0].sum()/losses) if losses else None,
                             additive_drawdown_bps=float((np.maximum.accumulate(equity)-equity).max()))
    daily = events.assign(net=events.gross_bps-17.8,
                          day=events.time.str[:10]).groupby('day').net.agg(['sum', 'count'])
    rng = np.random.default_rng(42)
    if len(daily) >= 2:
        draws = rng.integers(0, len(daily), size=(1000, len(daily)))
        means = daily['sum'].to_numpy()[draws].sum(axis=1)/daily['count'].to_numpy()[draws].sum(axis=1)
        out['daily_bootstrap_mean_ci95_bps'] = np.quantile(means, [.025, .975]).tolist()
    return out


def run(downloads: Path, output: Path, contract: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    frozen = contract.read_bytes()
    (output/'CONTRACT.md').write_bytes(frozen)
    report: dict[str, Any] = dict(can_trade=False, can_promote=False, performance_eligible=False,
        contract_sha256=hashlib.sha256(frozen).hexdigest(), code_sha256=sha(Path(__file__)),
        source='delta_official_trade_archive_unverified', funding_included=False,
        timestamp_timezone='UTC_ASSUMED', inputs=[], results=[], coverage={})
    for symbol in ('BTCUSD', 'ETHUSD'):
        parts = []
        for month in range(1, 10):
            if symbol == 'ETHUSD' and month == 3:
                continue
            suffix = ' (1)' if symbol == 'BTCUSD' and month == 3 else ''
            path = downloads/f'futures-trades-monthly-{symbol}-2026-{month:02d}.csv{suffix}.zip'
            minutes, audit = aggregate_archive(path, symbol)
            parts.append(minutes)
            report['inputs'].append(audit)
            print(f'{symbol} month {month}: {audit["rows"]} rows', flush=True)
        minutes = pd.concat(parts).sort_index()
        b = features(five_minutes(minutes))
        b.to_parquet(output/f'{symbol}_research_bars.parquet')
        report['coverage'][symbol] = dict(calendar_bars=len(b), observed_five_minute_bars=int(b.eligible.sum()),
                                         missing_or_partial_bars=int((~b.eligible).sum()))
        for family in FAMILIES:
            train, _ = replay(b, family, 'baseline', *PERIODS['train'], set())
            sessions = set()
            for (side, block), p in train.groupby(['side', 'block']):
                if len(p) >= 30 and p.time.str[:10].nunique() >= 10 and p.gross_bps.mean() > 17.8:
                    sessions.add((int(side), int(block)))
            for variant in VARIANTS:
                for period, bounds in PERIODS.items():
                    events, counts = replay(b, family, variant, *bounds, sessions)
                    events.to_json(output/f'{symbol}_{family}_{variant}_{period}.jsonl', orient='records', lines=True)
                    report['results'].append(dict(symbol=symbol, family=family, variant=variant,
                        period=period, allowed_session_cells=sorted(sessions), **counts, **metrics(events)))
        (output/'report.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    print(f'Report: {output / "report.json"}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--downloads', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--contract', type=Path, required=True)
    args = parser.parse_args()
    run(args.downloads, args.output, args.contract)
