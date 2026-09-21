"""Research translation of user-supplied SATS v1.13.1 default signal/ledger logic.

Not a Pine runtime, registered strategy, or execution adapter. Attribution:
WillyAlgoTrader. Nondefault calibration, dynamic targets and display omitted.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vnedge.research.archive_scanner_screen import PERIODS, sha


@dataclass(frozen=True)
class Settings:
    atr_len: int
    mult: float
    er_len: int
    sl_mult: float
    tick: float


def settings(symbol: str, tf: int, preset: str) -> Settings:
    tick = {'BTCUSD': .5, 'ETHUSD': .05}[symbol]
    if preset == 'Crypto 24/7':
        return Settings(14, 2.8, 20, 2.5, tick)
    if preset != 'Auto' or tf not in (5, 15):
        raise ValueError('unsupported_preset_or_timeframe')
    return Settings(10, 1.5, 14, 1., tick) if tf == 5 else Settings(14, 2., 20, 1.5, tick)


def rma(values: np.ndarray, length: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    if len(values) >= length:
        result[length-1] = np.mean(values[:length])
        for i in range(length, len(values)):
            result[i] = (result[i-1]*(length-1)+values[i])/length
    return result


def signal_frame(bars: pd.DataFrame, cfg: Settings) -> pd.DataFrame:
    b = bars.copy()
    if not b.index.is_monotonic_increasing or not b.index.is_unique:
        raise ValueError('bar_identity')
    if len(b) > 2 and b.index.to_series().diff().iloc[1:].nunique() != 1:
        raise ValueError('calendar_must_include_gaps')
    for name in ('atr', 'tqi', 'lower', 'upper', 'pivot_low', 'pivot_high'):
        b[name] = np.nan
    b['trend'], b['signal'], b['char_flip'] = 1, 0, False
    segments = (~b.eligible).cumsum()
    for _, p in b[b.eligible].groupby(segments[b.eligible]):
        n = len(p)
        c, h, lo = (p[k].to_numpy(dtype=float) for k in ('close', 'high', 'low'))
        previous = np.r_[c[0], c[:-1]]
        tr = np.maximum(h-lo, np.maximum(abs(h-previous), abs(lo-previous)))
        raw = rma(tr, cfg.atr_len)
        baseline = pd.Series(raw).rolling(100).mean().to_numpy()
        ratio = np.divide(raw, baseline, out=np.ones(n), where=np.isfinite(baseline) & (baseline != 0))
        changes = pd.Series(c).diff()
        denom = changes.abs().rolling(cfg.er_len).sum().to_numpy()
        numer = pd.Series(c).diff(cfg.er_len).abs().to_numpy()
        er = np.divide(numer, denom, out=np.zeros(n), where=np.isfinite(denom) & (denom != 0))
        atr = raw*(.5+.5*er)
        highest = pd.Series(h).rolling(20, min_periods=1).max().to_numpy()
        lowest = pd.Series(lo).rolling(20, min_periods=1).min().to_numpy()
        position = np.divide(c-lowest, highest-lowest, out=np.full(n, .5), where=highest != lowest)
        up = changes.gt(0).rolling(10).sum().fillna(0).to_numpy()/10
        down = changes.lt(0).rolling(10).sum().fillna(0).to_numpy()/10
        direction = pd.Series(c).diff(10).to_numpy()
        momentum = np.where(direction > 0, up, np.where(direction < 0, down, 0.))
        tqi = np.clip(.35*er + .20*np.clip((ratio-.6)/1.2, 0, 1) +
                      .25*np.clip(abs(position-.5)*2, 0, 1) + .20*momentum, 0, 1)
        sym = cfg.mult*(1+.5*(.5-er))*(.6+.4*(.6+.8*(1-tqi)**1.5))
        active_raw, passive_raw = sym*(1-.15*tqi), sym*(1+.2*tqi)
        active = pd.Series(active_raw).ewm(alpha=.15, adjust=False).mean().to_numpy()
        passive = pd.Series(passive_raw).ewm(alpha=.15, adjust=False).mean().to_numpy()
        lower, upper = np.full(n, np.nan), np.full(n, np.nan)
        trend, signal, char = np.ones(n, dtype=int), np.zeros(n, dtype=int), np.zeros(n, dtype=bool)
        pl, ph = np.full(n, np.nan), np.full(n, np.nan)
        last_low = last_high = np.nan
        low_i = high_i = -1000
        trend_start = 0
        warmup = max(50, cfg.atr_len+100-2)
        for i in range(n):
            prev = trend[i-1] if i else 1
            lr = c[i]-(active[i] if prev == 1 else passive[i])*atr[i]
            ur = c[i]+(passive[i] if prev == 1 else active[i])*atr[i]
            lower[i] = max(lr, lower[i-1]) if i and np.isfinite(lower[i-1]) and c[i-1] > lower[i-1] else lr
            upper[i] = min(ur, upper[i-1]) if i and np.isfinite(upper[i-1]) and c[i-1] < upper[i-1] else ur
            price_up = i > 0 and prev == -1 and c[i] > upper[i-1]
            price_down = i > 0 and prev == 1 and c[i] < lower[i-1]
            collapse = i >= 5 and i-trend_start >= 5 and max(tqi[i-4:i+1]) > .55 and tqi[i] < .25
            char_up = collapse and prev == -1 and c[i] > c[i-5]
            char_down = collapse and prev == 1 and c[i] < c[i-5]
            trend[i] = 1 if price_up or char_up else -1 if price_down or char_down else prev
            if trend[i] != prev:
                trend_start = i
                if i >= warmup and raw[i] > 0 and baseline[i] > 0:
                    signal[i] = trend[i]
                    char[i] = (char_up and not price_up) or (char_down and not price_down)
            # Rightmost equal extreme: pivot becomes known only on i, never i-3.
            if i >= 6:
                j = i-3
                if h[j] >= max(h[j-3:j]) and h[j] > max(h[j+1:i+1]):
                    last_high, high_i = h[j], j
                if lo[j] <= min(lo[j-3:j]) and lo[j] < min(lo[j+1:i+1]):
                    last_low, low_i = lo[j], j
            if i-low_i <= 100 and last_low < c[i]:
                pl[i] = last_low
            if i-high_i <= 100 and last_high > c[i]:
                ph[i] = last_high
        for key, val in dict(atr=atr, tqi=tqi, lower=lower, upper=upper, trend=trend,
                             signal=signal, char_flip=char, pivot_low=pl, pivot_high=ph).items():
            b.loc[p.index, key] = val
    return b


def down_tick(value: float, tick: float) -> float:
    return math.floor(value/tick+1e-9)*tick


def up_tick(value: float, tick: float) -> float:
    return math.ceil(value/tick-1e-9)*tick


def nearest_tick(value: float, tick: float) -> float:
    return math.floor(value/tick+.5)*tick


def plan(row: Any, cfg: Settings) -> dict[str, Any] | None:
    side = int(row.signal)
    entry = nearest_tick(row.close, cfg.tick)
    pivot = row.pivot_low if side == 1 else row.pivot_high
    if not np.isfinite(pivot):
        pivot = row.low if side == 1 else row.high
    buffer, cap = cfg.sl_mult*row.atr, max(4., cfg.sl_mult)*row.atr
    if side == 1:
        stop = down_tick(max(min(pivot-buffer, entry-buffer), entry-cap), cfg.tick)
    else:
        stop = up_tick(min(max(pivot+buffer, entry+buffer), entry+cap), cfg.tick)
    risk = side*(entry-stop)
    if risk < 2*cfg.tick-cfg.tick*1e-6 or side*(row.close-stop) <= cfg.tick*1e-6:
        return None
    targets = [nearest_tick(entry+side*max(cfg.tick, down_tick(risk*r, cfg.tick)), cfg.tick) for r in (1, 2, 3)]
    return dict(side=side, entry=entry, stop=stop, targets=targets, tqi=float(row.tqi))


@dataclass
class Trade:
    side: int
    entry: float
    stop: float
    targets: list[float]
    risk: float
    entry_i: int
    time: str
    tqi: float
    fee_bps: float
    remaining: float = 1.
    pnl: float = 0.
    fees: float = 0.
    hits: list[bool] = field(default_factory=lambda: [False]*3)

    def fill(self, price: float, fraction: float) -> None:
        if fraction > self.remaining+1e-8:
            raise ValueError('overfill')
        self.pnl += fraction*self.side*(price-self.entry)
        self.fees += fraction*abs(price)*self.fee_bps/10000
        self.remaining = max(0., self.remaining-fraction)

    def record(self, reason: str, ts: pd.Timestamp) -> dict[str, Any]:
        return dict(time=self.time, exit_time=ts.isoformat(), side=self.side,
                    entry=self.entry, stop=self.stop, targets=self.targets,
                    reason=reason, tqi=self.tqi, net_r=(self.pnl-self.fees)/self.risk,
                    net_bps=(self.pnl-self.fees)/self.entry*10000, fees=self.fees,
                    remaining=self.remaining)


def intrabar(trade: Trade, row: Any, slip_bps: float) -> str | None:
    s = trade.side
    if (row.low <= trade.stop if s == 1 else row.high >= trade.stop):
        price = min(row.open, trade.stop) if s == 1 else max(row.open, trade.stop)
        trade.fill(price*(1-s*slip_bps/10000), trade.remaining)
        return 'SL'
    for j, target in enumerate(trade.targets):
        if not trade.hits[j] and (row.high >= target if s == 1 else row.low <= target):
            trade.hits[j] = True
            trade.fill(target, trade.remaining if j == 2 else 1/3)
            if j == 2:
                return 'TP3'
    return None


def simulate(b: pd.DataFrame, cfg: Settings, world: str, start: str, end: str
             ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if world not in ('source_close_gross', 'next_open_gross', 'next_open_cost', 'next_open_stress'):
        raise ValueError('execution_world')
    delayed = world != 'source_close_gross'
    fee = 5.9 if world in ('next_open_cost', 'next_open_stress') else 0.
    slip = 3. if world == 'next_open_cost' else 6. if world == 'next_open_stress' else 0.
    lower, upper = pd.Timestamp(start, tz='UTC'), pd.Timestamp(end, tz='UTC')
    step = b.index[1]-b.index[0]
    trade: Trade | None = None
    pending: dict[str, Any] | None = None
    pending_exit: str | None = None
    records: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()

    def enter(p: dict[str, Any], price: float, i: int, ts: pd.Timestamp) -> Trade | None:
        price = nearest_tick(price*(1+p['side']*slip/10000), cfg.tick)
        risk = p['side']*(price-p['stop'])
        if risk < 2*cfg.tick-cfg.tick*1e-6 or p['side']*(p['targets'][0]-price) < cfg.tick-cfg.tick*1e-6:
            counts['rejected_entry'] += 1
            return None
        counts['entries'] += 1
        return Trade(p['side'], price, p['stop'], p['targets'], risk, i, ts.isoformat(),
                     p['tqi'], fee, fees=abs(price)*fee/10000)

    for i, row in enumerate(b.itertuples()):
        ts = row.Index
        if not lower <= ts < upper:
            continue
        if not row.eligible:
            if trade is not None:
                records.append(trade.record('CENSORED_GAP', ts))
                counts['censored'] += 1
            if pending is not None:
                counts['pending_gap'] += 1
            trade, pending, pending_exit = None, None, None
            continue
        if delayed:
            if pending_exit is not None and trade is not None:
                trade.fill(row.open*(1-trade.side*slip/10000), trade.remaining)
                records.append(trade.record(pending_exit, ts))
                trade = None
            pending_exit = None
            if pending is not None:
                if trade is not None:
                    raise AssertionError('pending_entry_with_open_trade')
                trade = enter(pending, row.open, i, ts)
                pending = None
        if trade is not None and (delayed or i > trade.entry_i):
            reason = intrabar(trade, row, slip)
            if reason:
                records.append(trade.record(reason, ts+step))
                trade = None
            elif trade.side != row.trend or i-trade.entry_i >= 100:
                reason = 'FLIP' if trade.side != row.trend else 'TIMEOUT'
                if delayed:
                    pending_exit = reason
                else:
                    trade.fill(row.close, trade.remaining)
                    records.append(trade.record(reason, ts+step))
                    trade = None
        if row.signal and ts+step < upper:
            counts['signals'] += 1
            if trade is None or pending_exit is not None:
                p = plan(row, cfg)
                if p is None:
                    counts['rejected_plan'] += 1
                elif delayed:
                    pending = p
                else:
                    trade = enter(p, p['entry'], i, ts+step)
            else:
                counts['blocked_active'] += 1
    if trade is not None:
        records.append(trade.record('CENSORED_END', upper))
        counts['censored'] += 1
    if pending is not None:
        counts['pending_end'] += 1
    return records, dict(counts)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [r for r in records if not r['reason'].startswith('CENSORED')]
    if not closed:
        return dict(closed=0)
    values = np.array([r['net_r'] for r in closed])
    curve = np.r_[0., values.cumsum()]
    loss = -values[values < 0].sum()
    return dict(closed=len(closed), mean_r=float(values.mean()),
                mean_net_bps=float(np.mean([r['net_bps'] for r in closed])),
                win_fraction=float((values > 1e-8).mean()),
                profit_factor_r=float(values[values > 0].sum()/loss) if loss else None,
                closed_additive_drawdown_r=float((np.maximum.accumulate(curve)-curve).max()),
                reasons=dict(Counter(r['reason'] for r in closed)))


def run(parent: Path, source: Path, contract: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output/'CONTRACT.md').write_bytes(contract.read_bytes())
    report: dict[str, Any] = dict(source_sha256=sha(source), contract_sha256=sha(contract),
        code_sha256=sha(Path(__file__)), parent_report_sha256=sha(parent/'report.json'),
        pine_parity_verified=False, can_trade=False, can_promote=False,
        performance_eligible=False, data=[], results=[])
    for symbol in ('BTCUSD', 'ETHUSD'):
        path = parent/f'{symbol}_research_bars.parquet'
        data = pd.read_parquet(path)[['open', 'high', 'low', 'close', 'volume', 'eligible']]
        report['data'].append(dict(symbol=symbol, path=str(path), sha256=sha(path)))
        for tf in (5, 15):
            b = data
            if tf == 15:
                groups = data.resample('15min')
                b = groups.agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'})
                b['eligible'] = groups.eligible.sum().eq(3) & groups.size().eq(3)
            for preset in ('Auto', 'Crypto 24/7'):
                cfg = settings(symbol, tf, preset)
                f = signal_frame(b, cfg)
                for world in ('source_close_gross', 'next_open_gross', 'next_open_cost', 'next_open_stress'):
                    for period, bounds in PERIODS.items():
                        records, counts = simulate(f, cfg, world, *bounds)
                        name = f'{symbol}_{tf}_{preset.replace(" ", "_").replace("/", "-")}_{world}_{period}'
                        with (output/f'{name}.jsonl').open('w') as stream:
                            for record in records:
                                stream.write(json.dumps(record, allow_nan=False)+'\n')
                        report['results'].append(dict(symbol=symbol, tf=tf, preset=preset,
                            world=world, period=period, **counts, **summarize(records)))
                print(f'{symbol} {tf}m {preset} complete', flush=True)
    (output/'report.json').write_text(json.dumps(report, indent=2, allow_nan=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ('parent', 'source', 'contract', 'output'):
        p.add_argument('--'+flag, type=Path, required=True)
    a = p.parse_args()
    run(a.parent, a.source, a.contract, a.output)
