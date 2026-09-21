# Independent ST4H / DC20 research replay

Frozen before results. BTCUSD Delta India venue OHLC, research only.
Evaluate Jan1 2025–Sep19 2026 UTC (end exclusive Sep20), with Nov1 2024
warmup. Also independent flat-start90d and14d windows, pre-window indicator
warmup. No protected judgment data. Earlier quoted n/WR/PF/PnL are hypotheses.

Two independent process invocations, scanner-specific artifacts and books.
No shared lock, positions, confluence or webhook. No live allocation/cron.
No pooling returns as though both traded a shared $1,000 account.

ST4H: length14 true range, multiplier2.5, cushion.15ATR. Test Wilder ATR seeded
by SMA14 and rolling SMA14 separately (the provided wording permits both).
Initial trend+1, band=lower at first validATR, no initial flip. Last-flip index
starts at that seed; a flip requires index difference>=3. Update the band with
current lower/upper before testing strict close beyond cushioned band. Set new
band to current opposite basic band on flip. Trend/band continue updating while
in position and during skipped sessions. Session exclusion refers to CLOSE
hour0/4/8 UTC. ATR/flip history warms up before the evaluation boundary; positions
start flat at that boundary. Missing bars reset indicator warmup.

Only a valid flip can enter; stop=flipped band, minimum distance30 inclusive.
No entries while in position or on an exit bar. Stop-first ambiguous bars,
1R target, no trailing/opposite-flip exit. Asia restriction never blocks exits.
Entry at closed-bar close; earlier extremes of entry bar cannot stop it.

DC20: prior20 closed daily highs/lows, strict close breakout. Minimum range80,
minimum signal-close risk50. Target=signal close plus/minus full channel width.
No ADR, kill, daily time filter, or other scanner state. Stop/target only,
stop wins ties, no same-bar re-entry. Current incomplete bar WATCH only.

Literal cost model inherited from preceding requested tests: fixed$1,000 raw
entry notional, fees.00059 of each fill notional, $.50/BTC slippage each fill
deducted as cash. No compounding, exchange rounding, funding, risk-gateway or
order-book replay. Stops fill at specified levels even across gaps (optimistic
literal replay, not execution-grade). Close fills are modeled, not guaranteed.

Missing historical bar while holding => unresolved book, halt new positions;
never clear position to manufacture a flat restart. Final open trades stay
open, excluded from closed-trade PF/net totals; report inventory separately.
DD is realized closed-trade cash DD, not mark-to-market account DD.

Report every variant/window without choosing a winning ATR retrospectively.
Artifacts include source/code hashes, settings, trades, bar decisions, final
open/unresolved state. No activation or deployment follows a green replay.
