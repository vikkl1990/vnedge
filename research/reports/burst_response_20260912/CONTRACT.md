# Fixed exploratory screen: five-minute volume-burst response

Frozen before computing outcomes on 2026-09-12. This is a new **research
hypothesis**, not a registered scanner, an executable order backtest, or a
change to any operational strategy. All results, including failures, retained.

## Question and trial budget

After a large, high-participation five-minute move ending near its extreme,
does the next 15-minute price response continue or reverse?

Exactly four cells: continuation and reversal, each on Delta India BTCUSD and
ETHUSD. No threshold search, no choosing the better side after seeing results,
no maker-fill assumptions. Both claims must be reported even if both fail.

## Data and information clock

- Static VM export `/tmp/vnedge-htf-recheck.PInWIi`; September 1, 2026 00:00 UTC
  through September 11, 2026 00:00 UTC, end exclusive. Ten completed days.
- This window has already been inspected in other research. It is exploratory,
  **not untouched OOS**, even when split into two five-day reporting blocks.
- Use stored closed canonical one-minute bars, verified stored content hashes,
  `coverage_ok=true`, and `data_quality=ok`. No official fallback or repairs.
- Five-minute research bars require five consecutive eligible one-minute
  children. Invalid/missing children invalidate that parent, never carry it.
- Decision at five-minute close; preceding 12 complete consecutive five-minute
  bars provide the reference median quote notional. Decision bar is excluded
  from that baseline. This does not enable a one-minute live decision clock.
- Trade-side fields are deliberately excluded: current decoding can default
  an unknown seller role to buy. Total quote notional is not signed order flow.

## Frozen setup

All conditions required on the closed five-minute bar:

1. Quote notional at least **2 times** the preceding 12-bar median.
2. Absolute open-to-close body at least **10 basis points**.
3. Positive high-low range; an up bar closes in the top 20% of its range,
   a down bar in the bottom 20%.

Continuation measures a position in the body's direction. Reversal measures
the opposite direction. These are competing price-response claims, not proof
of resting liquidity, absorption, liquidation, or market-maker inventory.

## Outcome and exclusion policy

- Entry proxy: one-minute open **one minute after decision close**, allowing
  a fixed 60-second processing delay; no same-close fill.
- Exit proxy: one-minute open 15 minutes after entry. No stop/target optimizer.
- Require every eligible minute from decision close through exit, inclusive.
  A missing/invalid outcome window is **censored**, counted separately, never
  imputed as flat. Reserve the whole holding interval even when censored.
- Non-overlapping opportunities per symbol and claim; next decision must be
  at or after the prior scheduled exit. Skipped overlapping setups counted.
- Gross return = side × (exit / entry − 1) × 10,000, fixed entry notional.
- Base costs use repository `delta_scalp_v2`: 11.8 bps fee/GST plus 6 bps
  modeled execution friction = **17.8 bps**. Sensitivities: fee-only 11.8 and
  stress 23.8. Gate reserve is not subtracted as booked PnL.
- No observed BBO spread/queue, sizing, gateway, kernel, funding, liquidation,
  or capital compounding. Fixed costs approximate entry/exit notional fees;
  this is a response screen, not account-level PnL or execution parity.

## Reporting and decision

Report counts, gross/net mean, win rate, PF, median, chronological cumulative
net-bps drawdown, first/second block means, best-trade-removed mean, and
deterministic day-block-bootstrap mean interval (2,000 resamples, seed 20260912).
Include zero-trade days in bootstrap blocks. Ten days cannot establish durable
regime robustness; intervals are descriptive, not a promotion test.

Fewer than 30 measured events = insufficient sample. Nonpositive modeled net
mean = unsupported at this cost. A positive result is only a lead for a
future preregistered, untouched BBO-based test, never a promotion.
`can_trade=false`, `can_promote=false` for every output. A no-edge result is
an acceptable conclusion. No additional variants in this run.
