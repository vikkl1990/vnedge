"""Independent literal research books. No scanner registry or execution authority."""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd


def features(frame: pd.DataFrame, scanner: str, atr_kind: str = "wilder") -> pd.DataFrame:
    if scanner not in ("ST4H", "DC20") or atr_kind not in ("wilder", "sma"):
        raise ValueError("unknown frozen variant")
    b = frame.copy()
    ok = b.complete.fillna(False).astype(bool)
    if scanner == "DC20":
        b["hh"] = b.high.where(ok).shift().rolling(20).max()
        b["ll"] = b.low.where(ok).shift().rolling(20).min()
        b["rng"] = b.hh-b.ll
        h, l, prev = b.high.where(ok), b.low.where(ok), b.close.where(ok).shift()
        tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
        tr = tr.where(ok & prev.notna())
        for n in (14, 50):
            b[f"atr{n}"] = tr.rolling(n).mean()
            b[f"adr{n}"] = (h-l).rolling(n).mean()
        b["atr_ratio"] = b.atr14 / b.atr50.where(b.atr50 > 0)
        b["adr_ratio"] = b.adr14 / b.adr50.where(b.adr50 > 0)
        return b
    b["atr"], b["band"], b["trend"], b["flip"] = np.nan, np.nan, 0, False
    trs: list[float] = []
    prev_close = atr = band = None
    trend, last_flip = 1, 0
    for i, (ts, row) in enumerate(b.iterrows()):
        if not row.complete:
            trs, prev_close, atr, band, trend = [], None, None, None, 1
            continue
        tr = float(row.high-row.low)
        if prev_close is not None:
            tr = max(tr, abs(row.high-prev_close), abs(row.low-prev_close))
        prev_close = float(row.close)
        trs.append(tr)
        if len(trs) < 14:
            continue
        atr = float(np.mean(trs[-14:])) if atr is None or atr_kind == "sma" else (atr*13+tr)/14
        mid = (row.high+row.low)/2
        lower, upper = mid-2.5*atr, mid+2.5*atr
        flip = False
        if band is None:
            band, last_flip = lower, i
        elif trend == 1:
            band = max(lower, band)
            if row.close < band-.15*atr and i-last_flip >= 3:
                trend, band, last_flip, flip = -1, upper, i, True
        else:
            band = min(upper, band)
            if row.close > band+.15*atr and i-last_flip >= 3:
                trend, band, last_flip, flip = 1, lower, i, True
        b.loc[ts, ["atr", "band", "trend", "flip"]] = [atr, band, trend, flip]
    return b


VOL_MODES = ("off", "atr_exp", "adr_band", "skip_comp", "atr_mid")


def vol_ok(row: pd.Series, mode: str = "off") -> bool:
    if mode not in VOL_MODES:
        raise ValueError("unknown volatility mode")
    if mode == "off":
        return True
    ratio = row.atr_ratio if mode in ("atr_exp", "atr_mid") else row.adr_ratio
    if not np.isfinite(ratio):
        return False
    if mode == "atr_exp":
        return bool(ratio >= 1.)
    if mode == "atr_mid":
        return bool(.85 <= ratio <= 1.30)
    if mode == "adr_band":
        return bool(.75 <= ratio <= 1.40)
    return bool(ratio >= .75)


def signal(row: pd.Series, scanner: str, close_ts: pd.Timestamp, vol_mode: str = "off") -> dict:
    if not row.complete:
        return {"action": "WATCH"}
    if scanner == "ST4H":
        if not np.isfinite(row.band):
            return {"action": "WARMUP"}
        if close_ts.hour in (0, 4, 8):
            return {"action": "SKIP_ASIA"}
        if not row.flip:
            return {"action": "WAIT"}
        side, stop = int(row.trend), float(row.band)
        distance = abs(row.close-stop)
        if distance < 30 or side*(row.close-stop) <= 0:
            return {"action": "SKIP_RISK"}
        target = row.close+side*distance
    else:
        if not np.isfinite([row.hh, row.ll]).all():
            return {"action": "WARMUP"}
        if row.rng < 80:
            return {"action": "SKIP_RANGE"}
        if not vol_ok(row, vol_mode):
            return {"action": "SKIP_VOL"}
        side = 1 if row.close > row.hh else -1 if row.close < row.ll else 0
        if not side:
            return {"action": "WAIT", "notes": [name for name, hit in (
                ("SWEEP_HIGH", row.high > row.hh and row.close <= row.hh),
                ("SWEEP_LOW", row.low < row.ll and row.close >= row.ll)) if hit]}
        stop = float(row.ll if side == 1 else row.hh)
        if abs(row.close-stop) < 50:
            return {"action": "SKIP_RISK"}
        target = row.close+side*row.rng
    return {"action": "SIGNAL", "side": side, "entry": float(row.close),
            "stop": stop, "target": float(target), "qty": 1000/float(row.close),
            "entry_ts": close_ts.isoformat()}


def replay(frame: pd.DataFrame, scanner: str, start: pd.Timestamp, end: pd.Timestamp,
           atr_kind: str = "wilder", vol_mode: str = "off") -> dict:
    if vol_mode not in VOL_MODES or (scanner != "DC20" and vol_mode != "off"):
        raise ValueError("volatility variants belong to DC20 only")
    step = pd.Timedelta(hours=4) if scanner == "ST4H" else pd.Timedelta(days=1)
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError("sorted unique open times required")
    # Missing calendar intervals cannot disappear from rolling windows.
    frame = frame.reindex(pd.date_range(frame.index.min(), frame.index.max(), freq=step))
    frame["complete"] = frame.complete.fillna(False).astype(bool)
    b = features(frame, scanner, atr_kind)
    position = None
    unresolved = False
    events, trades = [], []
    for ts, row in b.loc[(b.index >= start) & (b.index < end)].iterrows():
        close_ts = ts+step
        if close_ts > end:
            continue
        event = {"bar_open": ts.isoformat(), "bar_close": close_ts.isoformat()}
        if scanner == "DC20":
            event.update(vol_mode=vol_mode, **{k: float(row[k]) if np.isfinite(row[k]) else None
                         for k in ("hh", "ll", "rng", "atr_ratio", "adr_ratio")})
        if unresolved:
            event["action"] = "UNRESOLVED"
        elif not row.complete:
            event["action"] = "WATCH"
            unresolved = position is not None
        elif position is not None:
            p = position
            stop_hit = row.low <= p["stop"] if p["side"] == 1 else row.high >= p["stop"]
            target_hit = row.high >= p["target"] if p["side"] == 1 else row.low <= p["target"]
            event["action"] = "MANAGE"
            if stop_hit or target_hit:
                ep = p["stop"] if stop_hit else p["target"]
                gross = p["qty"]*p["side"]*(ep-p["entry"])
                fees = p["qty"]*(ep+p["entry"])*.00059
                slip = p["qty"]*1.0
                reason = "stop_and_target" if stop_hit and target_hit else "stop" if stop_hit else "target"
                trades.append({**p, "exit": ep, "exit_bar_close": close_ts.isoformat(),
                               "reason": reason, "gross": gross, "fees": fees,
                               "slippage": slip, "net": gross-fees-slip})
                event.update(action="EXIT", reason=reason)
                position = None
        else:
            event.update(signal(row, scanner, close_ts, vol_mode))
            if event["action"] == "SIGNAL":
                position = {k: event[k] for k in ("side", "entry", "stop", "target", "qty", "entry_ts")}
        events.append(event)
    net = np.array([t["net"] for t in trades], dtype=float)
    curve = np.r_[0., net.cumsum()]
    loss = float(-net[net < 0].sum())
    return {"scanner": scanner, "atr_kind": atr_kind if scanner == "ST4H" else None,
            "start": start.isoformat(), "end_exclusive": end.isoformat(),
            "metrics": {"n": len(net), "win_rate": float((net > 0).mean()) if len(net) else None,
                        "pf": float(net[net > 0].sum()/loss) if loss else None,
                        "net": float(net.sum()), "gross": sum(t["gross"] for t in trades),
                        "realized_dd": float((np.maximum.accumulate(curve)-curve).max())},
            "open_position": position, "unresolved": unresolved, "trades": trades, "events": events}


def fetch4h(output: Path) -> pd.DataFrame:
    start, end = pd.Timestamp("2024-11-01", tz="UTC"), pd.Timestamp("2026-09-20", tz="UTC")
    rows = []
    cursor = start
    while cursor < end:
        stop = min(end, cursor+pd.Timedelta(hours=4*1500))
        url = ("https://api.india.delta.exchange/v2/history/candles?symbol=BTCUSD&resolution=4h"
               f"&start={int(cursor.timestamp())}&end={int(stop.timestamp())}")
        req = urllib.request.Request(url, headers={"User-Agent": "VNEDGE research"})
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read()
        data = json.loads(raw)
        if not data.get("success"):
            raise ValueError(data)
        receipt = {"url": url, "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat(),
                   "sha256": hashlib.sha256(raw).hexdigest(), "payload": data}
        (output/f"source_{cursor.date()}.json").write_text(json.dumps(receipt))
        rows.extend(data["result"])
        cursor = stop
    frame = pd.DataFrame(rows)
    frame.index = pd.to_datetime(frame.pop("time"), unit="s", utc=True)
    frame = frame[["open", "high", "low", "close", "volume"]].astype(float).sort_index()
    for _, group in frame[frame.index.duplicated(keep=False)].groupby(level=0):
        if (group.nunique() > 1).any():
            raise ValueError("conflicting duplicates")
    frame = frame[~frame.index.duplicated()].reindex(pd.date_range(start, end, freq="4h", inclusive="left"))
    frame["complete"] = frame.notna().all(axis=1)
    valid = frame.loc[frame.complete]
    if ((valid.high < valid[["open", "close", "low"]].max(axis=1)) |
        (valid.low > valid[["open", "close", "high"]].min(axis=1)) |
        (valid.close <= 0) | (valid.volume < 0)).any():
        raise ValueError("invalid OHLCV")
    return frame


def run(scanner: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    contract = Path("research/st4h_dc20_v1/CONTRACT.md")
    if scanner == "ST4H":
        frame = fetch4h(output)
        source = "Delta India official 4h OHLC; source receipts in this directory"
    else:
        source = "research/vela_family_v1/attempt_01/daily.parquet"
        frame = pd.read_parquet(source)
    frame.to_parquet(output/"bars.parquet")
    report = {"scanner": scanner, "source": source, "can_trade": False,
              "funding_included": False, "missing_bars": int((~frame.complete).sum()),
              "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
              "data_sha256": hashlib.sha256((output/"bars.parquet").read_bytes()).hexdigest(), "results": []}
    end = pd.Timestamp("2026-09-20", tz="UTC")
    for atr in (("wilder", "sma") if scanner == "ST4H" else ("wilder",)):
        for window, start in (("full", pd.Timestamp("2025-01-01", tz="UTC")),
                              ("90d", end-pd.Timedelta(days=90)), ("14d", end-pd.Timedelta(days=14))):
            result = replay(frame, scanner, start, end, atr)
            result["window"] = window
            (output/f"{atr}_{window}.json").write_text(json.dumps(result, indent=2, allow_nan=False))
            report["results"].append({k: v for k, v in result.items() if k not in ("events", "trades")})
            print(scanner, atr, window, result["metrics"], flush=True)
    (output/"report.json").write_text(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scanner", choices=("ST4H", "DC20"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.scanner, args.output)
