# Scalper research gauntlet

The user-endorsed proof path for scalping (do not skip steps):

1. **1-minute candle approximation** with harsh fees — `scalper_gauntlet.py`
2. **Tick/L2 recorder** — zero-risk data collection (`tick_recorder.py`, deployed)
3. **True microstructure backtest** — after ~2 weeks of recorded ticks
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
