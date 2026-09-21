"""Literal VELA A/B research replay. No runtime/order-manager authority."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Config:
    range_days: int = 1
    fill: str = "close"
    b_delta: str = "disabled"
    notional: float = 1000.
    fee: float = .00059
    slip: float = .50
    comp_min: float = .75

    def __post_init__(self) -> None:
        if (self.range_days not in (1, 10, 20) or self.fill not in ("close", "next_open")
                or self.b_delta not in ("disabled", "off", "cvd", "candle_proxy")):
            raise ValueError("invalid frozen variant")


@dataclass(frozen=True)
class Position:
    book: str
    side: int
    entry: float
    stop: float
    target: float
    break_line: float
    qty: float
    entry_ts: str


def daily_features(daily: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    b = daily.copy()
    complete = b.complete.fillna(False).astype(bool)
    h, l = b.high.where(complete), b.low.where(complete)
    b["yh"] = h.shift().rolling(cfg.range_days).max()
    b["yl"] = l.shift().rolling(cfg.range_days).min()
    b["rng"] = b.yh - b.yl
    b["adr14"] = (h-l).rolling(14).mean()
    b["adr50"] = (h-l).rolling(50).mean()
    b["comp"] = b.adr14 / b.adr50.replace(0, np.nan)
    prev = b.close.where(complete).shift()
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    b["atr14"] = tr.where(complete).rolling(14).mean()
    return b


def a_signal(row: pd.Series, cfg: Config) -> dict[str, object]:
    if not row.complete:
        return {"action": "WATCH"}
    if not np.isfinite([row.yh, row.yl]).all() or (cfg.comp_min > 0 and not np.isfinite(row.comp)):
        return {"action": "WARMUP"}
    if row.rng < 80:
        return {"action": "SKIP_RANGE"}
    if cfg.comp_min > 0 and row.comp < cfg.comp_min:
        return {"action": "SKIP_COMPRESSED"}
    side = 1 if row.close > row.yh else -1 if row.close < row.yl else 0
    if not side:
        notes = []
        if row.high > row.yh and row.close <= row.yh:
            notes.append("SWEEP_HIGH")
        if row.low < row.yl and row.close >= row.yl:
            notes.append("SWEEP_LOW")
        return {"action": "WAIT", "notes": notes}
    stop, line = (row.yl, row.yh) if side == 1 else (row.yh, row.yl)
    if abs(row.close-stop) < 50:
        return {"action": "SKIP_RISK"}
    return {"action": "SIGNAL", "side": side, "stop": float(stop),
            "break_line": float(line), "rng": float(row.rng)}


def make_position(book: str, side: int, entry: float, stop: float, target: float,
                  line: float, ts: pd.Timestamp, cfg: Config) -> Position:
    return Position(book, side, entry, stop, target, line, cfg.notional/entry, ts.isoformat())


def exit_rule(p: Position, row: pd.Series, *, kill: bool) -> tuple[float, str] | None:
    stop = row.low <= p.stop if p.side == 1 else row.high >= p.stop
    target = row.high >= p.target if p.side == 1 else row.low <= p.target
    if stop:
        return p.stop, "stop_and_target" if target else "stop"
    if target:
        return p.target, "target"
    if kill and p.side*(row.close-p.break_line) < 0:
        return float(row.close), "kill"
    return None


def settle(p: Position, price: float, ts: pd.Timestamp, reason: str, cfg: Config
           ) -> dict[str, object]:
    gross = p.qty*p.side*(price-p.entry)
    fees = p.qty*(p.entry+price)*cfg.fee
    slip = p.qty*cfg.slip*2
    return {**asdict(p), "exit": price, "exit_ts": ts.isoformat(), "reason": reason,
            "gross_usd": gross, "fees_usd": fees, "slippage_usd": slip,
            "net_usd": gross-fees-slip, "net_bps": (gross-fees-slip)/cfg.notional*10000}


def b_session(bars: pd.DataFrame, day: pd.Timestamp, cfg: Config
              ) -> tuple[list[dict[str, object]], str, Position | None]:
    expected = pd.date_range(day+pd.Timedelta(hours=7), periods=68, freq="15min")
    b = bars.reindex(expected)
    if not b.complete.iloc[:6].fillna(False).all():
        return [], "MISSING_BOX", None
    high, low = float(b.high.iloc[:6].max()), float(b.low.iloc[:6].min())
    if not 40 <= high-low <= 900:
        return [], "SKIP_BOX", None
    delta = np.sign(b.close-b.open)*b.volume if cfg.b_delta == "candle_proxy" else b.get("cvd_delta")
    position = None
    for i in range(6, len(b)):
        row, ts = b.iloc[i], b.index[i]+pd.Timedelta(minutes=15)
        if not bool(row.get("complete", False)) or pd.isna(row.get("complete")):
            return [], "UNRESOLVED_B" if position else "MISSING_BREAK_HISTORY", position
        if position is not None:
            outcome = exit_rule(position, row, kill=False)
            if outcome:
                return [settle(position, outcome[0], ts, outcome[1], cfg)], "DONE_TODAY", None
        else:
            side = 1 if row.close > high else -1 if row.close < low else 0
            if not side:
                continue
            stop = low if side == 1 else high
            risk = abs(row.close-stop)
            if risk < 25:
                return [], "SKIP_RISK", None
            if cfg.b_delta != "off" and (
                delta is None or delta.iloc[:6].isna().any() or pd.isna(delta.iloc[i])
                or side*delta.iloc[:6].sum() <= 0 or side*delta.iloc[i] <= 0
            ):
                return [], "DELTA_REJECT", None
            position = make_position("B", side, float(row.close), stop,
                                     float(row.close+side*risk), high if side == 1 else low, ts, cfg)
    if position is not None:
        return [settle(position, float(b.close.iloc[-1]), day+pd.Timedelta(days=1), "eod", cfg)], "DONE_TODAY", None
    return [], "NO_BREAK", None


def replay(daily: pd.DataFrame, intraday: pd.DataFrame, cfg: Config,
           start: pd.Timestamp, end: pd.Timestamp) -> dict[str, object]:
    b = daily_features(daily, cfg)
    a: Position | None = None
    pending: dict[str, object] | None = None
    a_unknown = b_unknown = False
    unresolved_b = None
    trades, events = [], []
    for day, row in b.loc[(b.index >= start) & (b.index < end)].iterrows():
        close_ts = day+pd.Timedelta(days=1)
        consumed = a is not None
        action = "WAIT"
        if pending is not None:
            consumed = True
            side, stop, line = int(pending["side"]), float(pending["stop"]), float(pending["break_line"])
            if np.isfinite(row.open):
                if side*(row.open-line) > 0 and side*(row.open-stop) > 0:
                    a = make_position("A", side, float(row.open), stop,
                                      float(row.open+side*float(pending["rng"])), line, day, cfg)
                    action = "OPEN"
                else:
                    action = "CANCEL_PENDING"
                pending = None
            else:
                a_unknown = True
        a_state_at_07 = "QUARANTINED" if a_unknown else "PENDING_OPEN" if pending else (
            "IN_LONG" if a.side == 1 else "IN_SHORT") if a else "FLAT"
        b_action = "DISABLED"
        if cfg.b_delta != "disabled":
            if a_state_at_07 != "FLAT":
                b_action = "A_NOT_FLAT"
            elif b_unknown:
                b_action = "QUARANTINED"
            else:
                b_trades, b_action, unresolved_b = b_session(intraday, day, cfg)
                trades.extend(b_trades)
                b_unknown = unresolved_b is not None
        if a_unknown:
            action = "QUARANTINED"
        elif not row.complete:
            action = "WATCH"
            # A developing final day is not a historical gap.
            if day < b.index[-1] and a is not None:
                a_unknown = True
                action = "UNRESOLVED_A"
        elif a is not None:
            outcome = exit_rule(a, row, kill=True)
            if outcome:
                trades.append(settle(a, outcome[0], close_ts, outcome[1], cfg))
                a, action = None, "EXIT"
            else:
                action = "MANAGE"
        elif not consumed:
            signal = a_signal(row, cfg)
            action = str(signal["action"])
            if action == "SIGNAL":
                if cfg.fill == "next_open":
                    pending = signal
                else:
                    side = int(signal["side"])
                    a = make_position("A", side, float(row.close), float(signal["stop"]),
                                      float(row.close+side*float(signal["rng"])),
                                      float(signal["break_line"]), close_ts, cfg)
        events.append({"day": day.isoformat(), "as_of": close_ts.isoformat(),
                       "A": action, "A_at_07": a_state_at_07, "B": b_action,
                       "A_position": asdict(a) if a else None})
    return {"config": asdict(cfg), "trades": trades, "events": events,
            "open_A": asdict(a) if a else None, "pending_A": pending,
            "unresolved_A": a_unknown, "unresolved_B": asdict(unresolved_b) if unresolved_b else None}


def stats(trades: list[dict[str, object]]) -> dict[str, object]:
    if not trades:
        return {"trades": 0}
    t = pd.DataFrame(trades).sort_values("exit_ts", kind="stable")
    net = t.net_usd.to_numpy()
    curve = np.r_[0., net.cumsum()]
    loss = -net[net < 0].sum()
    return {"trades": len(t), "gross_usd": float(t.gross_usd.sum()), "net_usd": float(net.sum()),
            "mean_net_bps": float(t.net_bps.mean()), "win_fraction": float((net > 0).mean()),
            "pf": float(net[net > 0].sum()/loss) if loss else None,
            "realized_cash_drawdown_usd": float((np.maximum.accumulate(curve)-curve).max())}


def fetch(resolution: str, start: pd.Timestamp, end: pd.Timestamp, output: Path) -> pd.DataFrame:
    rows = []
    cursor = start
    step = pd.Timedelta(days=1) if resolution == "1d" else pd.Timedelta(minutes=15)
    while cursor < end:
        until = min(cursor+step*1500, end)
        query = urllib.parse.urlencode({"symbol": "BTCUSD", "resolution": resolution,
                                       "start": int(cursor.timestamp()), "end": int(until.timestamp())})
        url = "https://api.india.delta.exchange/v2/history/candles?"+query
        with urllib.request.urlopen(url, timeout=20) as response:
            raw = response.read()
        payload = json.loads(raw)
        if not payload.get("success"):
            raise ValueError("Delta history request failed")
        receipt = {"url": url, "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat(),
                   "sha256": hashlib.sha256(raw).hexdigest(), "payload": payload}
        (output/f"{resolution}_{int(cursor.timestamp())}.json").write_text(json.dumps(receipt))
        rows.extend(payload["result"])
        cursor = until
        time.sleep(.25)
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame.time, unit="s", utc=True)
    cols = ["open", "high", "low", "close", "volume"]
    for col in cols:
        frame[col] = pd.to_numeric(frame[col], errors="raise")
    for _, group in frame.groupby("timestamp"):
        if len(group[cols].drop_duplicates()) > 1:
            raise ValueError("conflicting history overlap")
    frame = frame.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    frame = frame.loc[(frame.index >= start) & (frame.index < end), cols]
    if (not np.isfinite(frame.to_numpy()).all() or (frame[cols[:4]] <= 0).any().any()
            or (frame.high < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame.low > frame[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("invalid history OHLC")
    frame = frame.reindex(pd.date_range(start, end, freq=step, inclusive="left"))
    frame["complete"] = frame[cols].notna().all(axis=1)
    return frame


def run(output: Path, contract: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output/"CONTRACT.md").write_bytes(contract.read_bytes())
    receipts = output/"source"
    receipts.mkdir()
    end = pd.Timestamp("2026-09-20", tz="UTC")
    daily = fetch("1d", pd.Timestamp("2024-11-01", tz="UTC"), end, receipts)
    print("Daily downloaded", len(daily), "missing", int((~daily.complete).sum()), flush=True)
    intra = fetch("15m", pd.Timestamp("2025-01-01", tz="UTC"), end, receipts)
    daily.to_parquet(output/"daily.parquet")
    intra.to_parquet(output/"15m.parquet")
    report = {"source": "delta_india_official_ohlc_research_only", "can_trade": False,
              "can_promote": False, "funding_included": False,
              "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
              "daily_missing": int((~daily.complete).sum()),
              "intraday_missing": int((~intra.complete).sum()), "results": []}
    for window, start in (("full", pd.Timestamp("2025-01-01", tz="UTC")),
                          ("90d", end-pd.Timedelta(days=90)), ("14d", end-pd.Timedelta(days=14))):
        for n in (1, 10, 20):
            for fill, delta in (("close", "disabled"), ("next_open", "disabled"),
                                ("close", "off"), ("close", "candle_proxy")):
                cfg = Config(n, fill, delta)
                result = replay(daily, intra, cfg, start, end)
                result.update(window=window, start=start.isoformat(), end_exclusive=end.isoformat(),
                              metrics={book: stats([t for t in result["trades"] if book == "combined" or t["book"] == book])
                                       for book in ("A", "B", "combined")})
                key = f"{window}_{n}_{fill}_{delta}"
                (output/f"{key}.json").write_text(json.dumps(result, indent=2, allow_nan=False))
                report["results"].append({k: v for k, v in result.items() if k not in ("trades", "events")})
                print(key, result["metrics"], flush=True)
    (output/"report.json").write_text(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    run(args.output, args.contract)
