# HTF structure continuation scanner v1

Status: `RESEARCH_ONLY` / `SHADOW_OBSERVE` / no capital permission.

## Evidence input

The Delta order-history export supplied on 2026-08-24 contains 27 order rows.
Thirteen completed trades can be reconstructed from valid-priced opening and
realized-PnL closing rows; one duplicate/administrative ETH close row has no
execution price and is excluded.

- 12 ETH trades: 12 positive after the separately reported trading fees.
- Median ETH holding time: 24.9 minutes.
- Median ETH favorable entry-to-exit movement: 8.88 points / 37.05 bps.
- Mean ETH holding time: 32.1 minutes; longest: 91.8 minutes.
- One TRUMP long was negative after fees.
- Seven closing rows reported zero fee, so fee-waiver eligibility must remain
  fill-derived; it cannot be assumed by the scanner.

The export contains fills, not the intervening price path. It therefore cannot
prove MAE, MFE, whether an exit was early, or whether a 3-4 point retracement
occurred. Those questions require canonical tick/candle replay around each
trade. The sample is behavioral context, not promotion evidence.

## Causal lifecycle

```text
fully closed 4h direction
  + confirmed closed 1h structure
  + no opposing dual-AVWAP bias
            |
            v
closed 15m pullback into EMA20
  + reclaim in HTF direction
  + close remains beyond EMA50
  + body and volume floors
            |
            v
one-sided arm above/below setup bar
            |
            v
3 distinct live BBO samples / >=3 seconds / <=8 bps chase
            |
            v
CostGate -> sizing -> shadow risk gateway -> virtual position
            |
            v
tick hard stop + closed-bar structure deterioration
  + fee-aware lock after 1.25R
  + ATR trail after 2R
  + 12h maximum hold
```

## Frozen setup contract

| Component | v1 rule |
|---|---|
| HTF direction | Confirmed 4h and 1h structure must agree |
| Trigger timeframe | Closed 15m setup; current quote entry |
| Pullback | Bar touches EMA20 within 0.35 ATR, then reclaims it |
| Trend floor | Setup close must remain beyond EMA50 |
| Meaning floor | Body >= 4 bps and volume >= 0.8x prior 96-bar median |
| Entry | 2 bps beyond setup high/low; 3 quotes held for 3 seconds |
| Stop | Farther of 1.25 ATR or buffered setup-bar structure |
| Profit cap | None |
| Lock/trail | Fee-aware lock after 1.25R; 1.5 ATR trail after 2R |
| Deterioration | Opposite confirmed 1h/4h direction or two closes through EMA50 |
| Max hold | 48 x 15m = 12 hours |
| Re-entry | 8-bar loss cooldown; 16-bar win cooldown; max 2/day |

## Explicit non-actions

- One adverse tick, one small dip, or one close through EMA50 does not flip.
- The scanner never emits both long and short arms from the same setup.
- A structure exit returns the lane to cooldown; it does not open the opposite
  side.
- Missing HTF alignment, pullback, body, volume, projected room, or data
  quality produces a named no-trade state.
- Shadow results do not grant promotion or order authority.

## Evidence still required

1. Backfill canonical 15m/1h/4h bars and quote/tick events for the selected
   period.
2. Replay the exact v1 arm, quote acceptance, stop, deterioration, and trail.
3. Report gross bps, fee/funding/slippage, MAE, MFE, capture ratio, exit reason,
   and no-trade gate histograms by symbol.
4. Run an untouched chronological window after any exploratory report.
5. Keep `CAPITAL_APPROVED` empty unless a separate reviewed promotion change is
   made.

