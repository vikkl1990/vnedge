"""Full-chain parity: SqueezeObserveRunner (strategy columns + engines)
must reproduce research.squeeze_trigger_replay on the same tape.

This is the merge gate before SHADOW_OBSERVE uses the runner: same bars ->
same fires, same exit reasons, same net bps.  Deterministic synthetic tape,
no network.  Note: the strategy ranks with pandas average-rank while the
replay uses strict-less counting -- identical except under exact float ties,
so the tape uses continuous noise (no ties).
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from research.squeeze_trigger_replay import replay
from vnedge.runtime.squeeze_observe import SqueezeObserveRunner
from vnedge.strategy.squeeze_expansion_breakout import PARAMS, SqueezeExpansionBreakout

UTC = dt.timezone.utc
T0 = 1_700_000_000_000


class _ListJournal:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def append(self, kind: str, payload: dict) -> None:
        self.records.append((kind, payload))


def _tape() -> list[tuple]:
    rng = np.random.default_rng(3)
    warm = PARAMS.rank_lookback_bars + PARAMS.compression_bars + 20
    base = 60_000 + np.cumsum(rng.normal(0, 25.0, warm))
    tail = base[-1] + np.cumsum(rng.normal(0, 1.2, PARAMS.compression_bars + 6))
    burst_base = float(tail[-1])
    # expansion leg: strong impulse bars that clear the box, then continuation
    leg = [burst_base * (1 + k * 18 / 10_000) for k in range(1, 10)]
    fade = [leg[-1] * (1 - k * 6 / 10_000) for k in range(1, 30)]
    closes = np.concatenate([base, tail, leg, fade])
    n = len(closes)
    volumes = np.full(n, 100.0) + rng.normal(0, 3.0, n)
    volumes[warm + PARAMS.compression_bars + 6 : warm + PARAMS.compression_bars + 15] = 300.0
    spread = closes * 0.0006
    return [
        (
            T0 + i * 300_000,
            float(closes[i]),
            float(closes[i] + spread[i]),
            float(closes[i] - spread[i]),
            float(closes[i]),
            float(max(volumes[i], 1.0)),
        )
        for i in range(n)
    ]


def test_runner_matches_replay_on_same_tape() -> None:
    bars = _tape()
    eval_start = bars[0][0]

    replay_trades = replay("TEST", bars, eval_start)
    assert replay_trades, "tape must produce at least one replay trade"

    frame = pd.DataFrame(
        {
            "open": [b[1] for b in bars],
            "high": [b[2] for b in bars],
            "low": [b[3] for b in bars],
            "close": [b[4] for b in bars],
            "volume": [b[5] for b in bars],
        }
    )
    prepared = SqueezeExpansionBreakout().prepare(frame)
    journal = _ListJournal()
    runner = SqueezeObserveRunner(journal=journal, symbol="TEST")
    for i in range(len(prepared)):
        ts = dt.datetime.fromtimestamp(bars[i][0] / 1000, UTC)
        runner.on_prepared_bar(prepared, i, ts)

    outcomes = [p for k, p in journal.records if k == "shadow_outcome"]
    intents = [p for k, p in journal.records if k == "shadow_intent"]
    assert len(intents) == len(replay_trades)
    assert len(outcomes) == len(replay_trades)
    for got, want in zip(outcomes, replay_trades, strict=True):
        assert got["side"] == want["side"]
        assert got["resolution"] == want["reason"]
        assert got["entry_price"] == pytest_approx(want["entry"])
        assert got["exit_price"] == pytest_approx(want["exit"])
        net_bps_got = got["virtual_net_usd"] / runner.notional_usd * 1e4
        assert abs(net_bps_got - want["net_bps"]) < 1e-6


def pytest_approx(value: float):
    import pytest

    return pytest.approx(value, rel=1e-9, abs=1e-9)
