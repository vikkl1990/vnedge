# Scalper research gauntlet

The user-endorsed proof path for scalping (do not skip steps):

1. **1-minute candle approximation** with harsh fees — `scalper_gauntlet.py`
2. **Tick/L2 recorder** — zero-risk data collection (`tick_recorder.py`, deployed)
3. **True microstructure backtest** — replay recorded ticks through
   `replay_backtester.py`
4. **Paper trial** — only if it clears cost through walk-forward + human approval

## Step 1 result (2026-07-04): candle scalper has no gross edge

`python -m vnedge.research.scalper_gauntlet --days 45` on BTC 1m, fee sweep:

| taker bps | OOS net | fees | trades |
|---|---|---|---|
| 0.5 | −$779.98 | $228 | 912 |
| 1.0 | −$860.46 | $387 | 912 |
| 2.0 | −$943.92 | $581 | 912 |
| 5.0 | −$996.33 | $789 | 912 |

**It loses at 0.5bps — below any real maker rate.** At near-zero fees the loss
is −$780 with only $228 in fees, so ~$550 is *gross* signal loss. Breakeven
fee is negative. Conclusion: the candle flow-proxy (close-in-range + volume +
momentum) has negative gross expectancy; fees only deepen it.

## What this does and does NOT prove

- DOES: the candle *approximation* of a scalper is not worth pursuing.
- Does NOT: that the real microstructure scalper has no edge. Candles can't
  see book_imbalance or taker-buy flow — the actual signals in
  `src/vnedge/scalping/features.py`. A weak proxy failing is not proof the
  true signal fails.

That gap is exactly why step 2 exists. The tick recorder is now collecting
real trades + top-of-book on the VM; after enough data we replay the genuine
microstructure scalper (step 3). Until then: scalping stays unproven, assume
negative, no live/paper exposure.

Measurement caveat: the gauntlet relaxes the 5x leverage cap so tight-stop
scalp trades execute (isolating the fee variable). Live sizing would throttle
it further — another headwind, not a tailwind.

## Step 3 scaffold (2026-07-04): conservative tick replay engine

`src/vnedge/scalping/replay_backtester.py` now replays recorded trades +
top-of-book snapshots through the same `TopOfBook`, `TradeTick`, and
`IncrementalFeatureEngine` used by the event-driven scalper foundation.

Rules are deliberately strict:

- Entry is a post-only maker quote at the favored touch.
- A buy quote fills only when seller flow trades through the bid; a sell quote
  fills only when buyer flow trades through the ask.
- Exits are taker exits at the actually tradable opposite touch. Long targets
  require the bid to reach target; short targets require the ask to reach
  target. The engine does not award phantom exits through the spread.
- Unfilled quotes expire on the first later event at or beyond TTL and count
  as missed fills; stale quotes cannot fill just because the next event is a
  trade.
- Quotes still alive when the replay window ends are counted separately as
  open-at-end, not mislabeled as expired misses.
- End-of-window forced exits use the tradable bid/ask touch, never mid.
- Invalid/crossed book records and invalid trade sides are skipped, not
  coerced into useful-looking signals.

This is not a promotion gate yet. It is the proof engine we run once the tick
recorder has enough real data. The principle stays the same: no live or paper
scalper exposure until the strategy clears costs on untouched replay data.

## Step 3 diagnostic command (2026-07-05): explain signal silence

Use the diagnostic wrapper when the scalper appears quiet:

```bash
python -m vnedge.research.scalper_replay_diagnostics --day YYYYMMDD
```

It runs a small replay sweep across imbalance/spread thresholds and labels the
primary blocker:

- `NO_TICK_DATA` - no usable trade/book recorder files.
- `NO_QUOTES` - book/spread filters never produce passive quotes.
- `NO_FILLS` - quotes are placed, but conservative through-fill rules do not
  fill them.
- `NEGATIVE_EDGE_AFTER_COST` - fills happen, but maker/taker/slippage costs
  beat the scalp.
- `UNDER_SAMPLED_TICKS` / `UNDER_SAMPLED_POSITIVE` - keep recording; the sample
  is too thin to promote or reject decisively.
- `CANDIDATE_FOUND` - still research only; pre-register an untouched replay
  window before shadow/paper exposure.

First local BTC sample (`20260704`, Binance USD-M) was only 16 minutes:
19,030 usable events, 9,424 book updates, 9,606 trades. The replay did place
quotes, but every sweep row remained negative after costs; best row was
83 quotes / 2 fills / -$0.112 on $100 notional. Primary blocker is therefore
`UNDER_SAMPLED_TICKS`, with a negative directional read from the rows we have.

Action: keep recording tick/L2 data. Do not loosen filters or route live/paper
signals just to create activity.

## Step 3 scanner layer (2026-07-05): find profitable scalp lanes

The scalper to build is not an always-on high-frequency bot. It is a
discovery-first, scanner-gated, maker/taker-aware microstructure scalper:

1. Discover active linear perp/future markets across venues.
2. Record tick/L2 data before judging a scalper lane.
3. Mine the recorded tape for pressure, absorption, and microprice hypotheses.
4. Rank lanes with scanners for liquidity, sample sufficiency, PF, route cost,
   fill evidence, adverse selection, and positive net bps after fees.
5. Replay untouched windows through the conservative maker-in/taker-out engine.
6. Only then run walk-forward / untouched judgment and human approval. Even a
   replay candidate is research-only until human-approved paper/shadow.

Run the scanner:

```bash
python -m vnedge.research.scalper_scanners \
  --exchanges binanceusdm,bybit \
  --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT \
  --days YYYYMMDD
```

Exchange-wide discovery is supported for the full active linear derivatives
universe:

```bash
python -m vnedge.research.scalper_scanners \
  --all-markets \
  --exchanges binanceusdm,bybit,delta \
  --quote-assets USDT,USDC,USD \
  --days YYYYMMDD
```

Use `--max-symbols-per-exchange N` for first-pass recorder allocation. Discovery
still includes the long tail, but prioritizes the default major universe before
obscure contracts when a cap is used.

Scanner states:

- `MISSING_TICK_DATA` - start the public tick/L2 recorder.
- `RECORD_MORE` - lane has useful ingredients, but the sample is too short.
- `REPLAY_CANDIDATE` - research candidate only; pre-register untouched replay.
- `REJECTED_LIQUIDITY` - spread/depth conditions are not scalper-grade.
- `REJECTED_NO_QUOTES` - imbalance never becomes quote-worthy.
- `REJECTED_NO_FILLS` - passive quotes do not fill under conservative rules.
- `REJECTED_COST_WALL` - fills happen, but fees/slippage/adverse selection win.

First scanner read on the local BTC sample:

```text
state=RECORD_MORE priority=85.3 edge_score=50
spread_p95=0.016bps fills=2 fill_rate=2.4% avg_net=-5.61bps
action=record 5.7h more before judging
```

Interpretation: Binance BTC is worth recording because liquidity is excellent,
but it is not worth trading. The edge evidence is still negative and too short.

## Maker vs taker routing rule

The scanner now emits `route_decision` for the best replay row:

- `BLOCKED` - PF or avg net bps is below breakeven. No signal is valid.
- `MAKER_ONLY` - replay clears maker-entry economics, but not taker-entry cost.
- `TAKER_ALLOWED` - PF and avg net bps clear the extra taker-entry fee wall.

Defaults:

- Maker floor: PF >= 1.15 and avg net bps >= +0.5bps after replay costs.
- Taker floor: PF >= 1.80 and avg net bps must also cover the extra taker-entry
  fee versus maker entry.

This is the hard rule behind "bare minimum breakeven": a scanner row below
breakeven is a blocked research lane, not a weak trading signal.

## Edge miner (2026-07-05): prove the missing edge

The edge miner searches recorded tick/L2 tapes for non-candle hypotheses before
they become strategies:

- `pressure_continuation`: book imbalance and taker flow agree.
- `absorption_reversal`: resting liquidity absorbs opposite taker pressure.
- `microprice_continuation`: microprice is displaced from mid enough to imply
  short-horizon pressure.

Run:

```bash
python -m vnedge.research.scalper_edge_miner \
  --all-markets \
  --exchanges binanceusdm,bybit,delta \
  --quote-assets USDT,USDC,USD \
  --days YYYYMMDD \
  --limit 100
```

The miner evaluates forward horizons, subtracts maker-entry + taker-exit +
slippage costs, computes PF on net bps, and emits the same route decision:
`BLOCKED`, `MAKER_ONLY`, or `TAKER_ALLOWED`.

First BTC read (`20260704`) still shows the core problem:

```text
state=BELOW_BREAKEVEN
route=BLOCKED
best avg_forward ~= +0.60bps
best avg_net ~= -7.40bps
```

Interpretation: there is microstructure movement, but not enough to clear the
fee wall. This is the missing edge made measurable.
