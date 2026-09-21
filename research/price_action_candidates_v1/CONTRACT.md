# Three price-action candidates v1 — frozen before replay

Research only: BTCUSD/ETHUSD, supplied August 2026 trade archives. August has
already been seen in prior screens; every result is exploratory. No optimization,
untouched judgment data, runtime registry, paper/live activation or promotion.

Source: archive timestamps assumed UTC, contracts converted BTC .001 / ETH .01
base. Inputs hashed; no trade IDs or independently certified completeness.
Five observed minutes required per 5m bar. Missing calendar bars remain invalid.
No OHLC proxies for VWAP: sum trade notional / sum base since UTC midnight.
VWAP usable only with every minute observed since midnight (not certified
transport completeness). Volume and notional must both be available.

Features reset across invalid bars: 50-bar warmup, EMA20/50, mean TR20, prior20
high/low and median volume. All decisions at closed 5m bar; next-open entry.
Both sides symmetric; simultaneous long and short conditions abstain.

1. sweep_reclaim_v1: preceding bar breached its prior20 low/high and closed
   back inside, directional body. Current close exceeds sweep high (long) or
   low (short), with directional body. This is one-bar confirmation, not an
   institutional-order claim.
2. session_vwap_bounce_v1: three preceding closes above/below their session
   VWAPs. Current wick comes within .20 ATR of VWAP, penetrates no more than
   .50 ATR; directional recovery closes above/below VWAP. Body/range >=.50,
   close in outer quarter of range, volume >=1.20 prior20 median. Prior-bar
   volume <= prior20 median. Entry no further than 1 ATR from decision VWAP.
   No unconfirmed pivots, no inferred aggressor selling.
3. consolidation_breakout_v1: prior20 range width <=4 prior ATR20, closed price
   breaks its boundary, volume >=1.50 prior20 median. No VWAP condition.

Same execution for all: initial stop 1.5 decision ATR from modeled next-open
fill, target 2R, maximum12 bars including entry bar. Stop wins if both touched.
Adverse stop gaps fill at open; favorable target gaps credited only at target.
Timeout at twelfth bar close. One open trade per candidate/symbol/cost scenario.
No overlapping fills; reserve the full horizon for censored outcomes. No fills
through gaps or beyond archive end. A new decision can occur at an exit close.

Costs: gross diagnostic; base 5.9bps per fill +3bps adverse slippage per side;
stress same fee +6bps slippage. These are frozen research assumptions, NOT a
verified current exchange schedule. Fees on entry and exit notional. No funding,
spread/order-book/latency replay, lot sizing, risk gateway or paper-order claim.
Candidate entry distances use next-open modeled fill, not future candle range.

Report every candidate/symbol/cost scenario: candidates, overlap, censored,
entry-distance rejects, trades, gross/net mean bps, net win fraction, profit
factor, cumulative unweighted trade-bps drawdown (not account drawdown), by-side
and fixed UTC six-hour entry blocks. No selection by session/regime after test.
No trade-count or green-return result can auto-activate a scanner.
