# Pre-registration: `squeeze_expansion_breakout_v2` — arm × gate ablation

Status: **RESEARCH_ONLY**
Frozen: 2026-08-19
Timeframe: closed 5m canonical candles
Capital permission: none (`CAPITAL_APPROVED` remains empty regardless of outcome)

## Hypothesis

The trigger and exit planes, not the compression arm, carry whatever edge this
system has. Two gates inside the trigger plane may be costing more than they
save. Specifically:

1. **Arm** — an ignition arm (wide body on heavy volume clearing a recent box)
   may retain more after-cost edge than the coil arm (compression-rank break),
   which is structurally blind to 85–90% of expansion events.
2. **VWAP side veto** — refusing breaks that oppose the 24h VWAP is the largest
   tunable blocker after a real break (127 of 172 post-break refusals for coil,
   282 of 496 for ignition) and may be removing more winners than losers.
3. **Chase cap** — capping entry distance at 20 bps past the level may be
   rejecting the strongest breaks rather than the latest ones.

Negative scope: this tests **gates on an existing plane**. It does not test new
indicators, new timeframes, additional symbols, or any change to the exit
profile. It cannot and does not test whether the system captures expansion
events; that KPI is explicitly excluded (see Non-goals).

## Frozen rules

| Field | Value |
|---|---:|
| Arms under test | `coil`, `ignition` |
| Coil arm | box 48 bars, rank ≤ 0.20 over 2016 bars, 1 fire/episode |
| Ignition arm | body ≥ 60% of range, volume ≥ 2.5× MA(48), 2h box |
| Chase cap values | 20 bps (ship), 40 bps |
| VWAP side veto | on, off |
| Break confirmation | bar close beyond box ± 2 bps buffer |
| Volume confirm (trigger) | 1.3× MA(48) — FROZEN, not under test |
| Fires per UTC day | 4 — FROZEN |
| Spacing / cooldown | 18 bars / 45m loss, 20m win — FROZEN |
| Stop | level − 1.7 × ATR(48), level-anchored — FROZEN |
| Exit profile | SCALP: failed-breakout, no-progress 4 bars, BE @ +1R, trail @ +2R, 4h backstop — **FROZEN, not under test** |
| Cost | Delta all-in taker 5.9 bps/leg; Scalper Offer free close ≤ 30 min |
| Entry fill | level + 1 bp slippage |
| Symbols | BTCUSDT, ETHUSDT — judged **separately**, never pooled |
| Notional | $3,000 per lane |

Grid: 2 arms × 2 chase × 2 vwap = **8 cells per symbol**. No other axis moves.

## Decision path

```text
closed 5m bar
  -> ArmSource.observe(ctx)            (coil | ignition)
  -> TriggerEngine.try_fire(...)       (gates under test: chase, vwap side)
     -> reject code recorded           (no_break | volume | vwap_side | chase_burn | ...)
  -> ExitEngine.on_bar(...)            (SCALP profile, frozen)
  -> ScannerTrade -> TrialLedger       (every cell recorded, winners and rejects)
virtual research outcome only; no OrderIntent, no capital path
```

## Validation design

| Segment | Dates | Use |
|---|---|---|
| Seen / research | 2026-05-21 → 2026-08-19 | diagnostics only — **already burned**, cannot produce a verdict |
| **Sealed tail** | 2026-02-19 → 2026-05-20 (90d, untouched) | the only pass/fail evidence |

The sealed tail is declared before the first run and is not inspected until the
grid executes once. One run. The verdict stands.

## Fixed acceptance and kill criteria

- Report per cell: n, PF, net bps, max drawdown, PSR, and both 45-day halves.
- Report family PBO and per-cell DSR from `TrialLedger.trial_count()` — the
  honest count including every discarded variant, not a remembered number.
- A cell is **interesting** only if: n ≥ 30, PF ≥ 1.25, positive net after full
  modeled cost, and both halves positive.
- A cell is **credible** only if additionally **DSR ≥ 0.95** and family
  **PBO ≤ 0.20**.
- Decision rules, declared now:
  - *ignition ≥ coil on the sealed tail* ⇒ coil is documented as an optional
    filter, not the product; the registered strategy keeps its id and params.
  - *chase 40 ≥ chase 20* ⇒ the cap may be loosened by a reviewed change, in a
    new config id; never silently.
  - *vwap off ≥ vwap on* ⇒ the veto is documented as net-negative and disabled
    in a new config id.
  - Any change to `be_arm_r` or the exit profile requires a **new strategy id**,
    not a config edit — the breakeven lock is twice-measured as load-bearing
    (−937 bps removed, −413 bps widened) and is out of scope here.
- No cell reaching *credible* ⇒ the ablation is inconclusive, everything stays
  frozen, and the window is burned.

## Power / honesty

At ~1.9 coil fires/day and ~4 ignition fires/day across two symbols, a 90-day
sealed tail yields roughly 170 coil and 370 ignition trades — adequate for PF
but **not** for distinguishing PF 1.25 from 1.40. If a cell lands under n = 30,
the result is **inconclusive, not a pass**, and filters must not be relaxed to
reach 30. Expect DSR to be the binding constraint: on seen data the shipped
baseline scored 0.155 at an estimated 60 trials, and the ledger will make the
real count larger, not smaller.

## Non-goals (forbidden during this test)

- No changes to: compression threshold or windows, volume multiplier, fires/day,
  spacing, cooldowns, stop multiplier, or any exit rule.
- No new arms, indicators, timeframes, or symbols.
- No scoring on expansion-event coverage (CAUGHT %), time-in-market, or
  day-capture. Those are measurement outputs, never objectives.
- No re-running a cell after seeing its result.

## Promotion path

Research replay → **this sealed ablation** → SHADOW_OBSERVE with matching reject
codes and costs → human-reviewed paper gate → `CAPITAL_APPROVED` edit. Nothing
here shortens that ladder. On failure, this document is archived to
`research/archive/squeeze_arm_gate_ablation_FAILED_<date>.md` with the numbers
intact and the spec unchanged.

## Burn registry

Append on completion, with the window hash, the grid, the `TrialLedger` window
key, and the verdict per cell.

## One-line decision rule

Only a cell with n ≥ 30, PF ≥ 1.25, both halves positive, DSR ≥ 0.95 and family
PBO ≤ 0.20 on the untouched tail may change any shipped value — and only by a
reviewed change to a new config id.
