# VELA family v1 — frozen test interpretation

Research only. Follow the user's corrected sections 6–9, not the broken paste.
12 separate runs: A yesterday/10d/20d × (close/B off, next_open/B off,
close/B delta off, close/B candle_proxy). CVD missing rejects; no proxy fallback.

Inputs: Delta India public BTCUSD venue OHLC, NOT canonical trade-lake bars.
Daily warmup from 2024-11-01; evaluated 2025-01-01 through 2026-09-19 UTC.
15m same evaluation interval. Also independent flat-start last90d and last14d
windows, with prior daily bars for indicators, never inherited positions.
The original 21-month dates are unspecified; report exact dates rather than
claiming exact replication. Check Aug21–Sep19 occupancy in the full-run journal.
No protected judgment window is consumed; every result exploratory.

A: prior1/10/20 days high/low, range >=80, ADR14/ADR50 >=.75, signal-close
distance to stop >=50. Close break strict. Current-day ADR allowed. One position.
Stop/target intrabar with stop-first tie; kill at daily close only. No entry
after manage/exit on same day. Close fills never use earlier same-day extremes.
Next-open: strict break_line/stop test; cancel consumes the day; if filled,
that day's extremes can stop/target it. Target recalculated from actual raw open.

B gates on A state at 07:00, using prior daily-close state after pending-open
processing. Box includes six 15m opens07:00..08:15; first candidate bar opens
08:30 (decision08:45). Box width40..900 inclusive; minR25. First outside close
consumes the daily attempt even on rejection. Later bars only manage fills.
EOD uses last15m close. Daily A processing happens after B EOD, no retroactive
B blocking. A daily management is not backdated to allow B after an intraday
A exit inferred only from the daily bar.

Book cash model: qty=1000/raw entry. Prices, stops, targets follow spec exactly.
Fee .00059 times notional EACH fill. Slippage $.50/BTC EACH fill deducted as
cash, without changing technical levels. No compounding. This literal stop
fill-at-level model can be optimistic across gaps; not execution-grade.
Funding, exchange lot rounding, spread/queue/latency and risk gateway excluded.
No live or paper orders, no runtime registration, no source/gate modifications.

Incomplete final daily bar => WATCH/no A management or entry. Interior daily
gaps => no new A signal; unknown open A outcome quarantines the book, never
silently resets occupancy. ADR/range require complete contiguous windows.
Missing B box => skip. Missing bar after B entry => unresolved trade, quarantine
B for the remainder of that run. Final open positions retained, not liquidated
to improve results; open exposure and realized-only statistics disclosed.

Report A and B and combined closed trades, PF, realized gross/net dollars,
mean net bps, realized-cash drawdown (not mark-to-market DD), skipped/blocked
days, state events, pending/open/unresolved inventory and gap coverage.
Quoted PF values/long occupancy are hypotheses, not golden test outputs.
