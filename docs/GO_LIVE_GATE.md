# funding_mr BTC — Go-Live Gate + Capacity Math (2026-08-07)

The bot has been in paper/shadow limbo because there was no written criterion
for when it goes live or gets shelved — so "one more scanner" always won. This
is that criterion. It applies to the one validated edge, **funding_mean_
reversion on binance BTC**. crypto_trend DOGE stays shadow-only until it earns
its own gate (it is high-variance and currently flat live).

Nothing here is financial advice. Real capital risk and the go/no-go call are
the operator's alone; these are objective system criteria and honest estimates.

---

## 1. Capacity math — is this worth real money?

Grounded in the actual record (research OOS + live shadow), with a live haircut.

| Input | Honest value | Source |
|---|---|---|
| Trade frequency | ~**30–40 real events/yr** | trial baseline: ≥10 trades needs ~90–120d; shadow counts are inflated by overlapping intents |
| Net expectancy / trade | ~**+$1.0–2.0** on a $500 book (~$12 risk/trade) | OOS +$16/31, +$55/22; live shadow +$79/17 (optimistic) |
| Gross annual (paper) | ~**$40–70 on $500 (≈8–12%)** | freq × expectancy |
| Live haircut | **−40%** (slippage, fill timing, funding settlement, the trap) | paper always overstates |
| **Net annual estimate** | **~$25–50 on $500 (≈5–10%)**; ~$50–100 on $1,000 | after haircut |

**The blunt read:** a positive, real, but **thin and sparse** edge. At the
<$1k design point that is **$25–100/year** — a *validated proof-of-concept*,
**not a business**. Its value is (a) proving the whole pipeline survives real
money end-to-end, and (b) a foundation to scale capital **only if** the live
edge holds. Decide up front which you're doing: banking a research win, or
building a base to scale. Do not confuse a 6% edge on $1k with income.

---

## 2. GO / NO-GO gate

**GO to `live_small` only if ALL hold:**
1. ≥ **30 completed paper trades** on funding_mr binance BTC — counting **distinct events**, not overlapping shadow intents (needs the dedup fix first).
2. Paper **profit factor ≥ 1.3** over those trades.
3. Paper **max drawdown ≤ 8%** of the paper book.
4. ≥ **4 distinct funding-extreme events** traded (not one lucky cluster).
5. Pre-live hardening (§3) all pass.
6. Mainnet **trade-only** keys installed + the three-gate live confirm armed.

**NO-GO / shelve if any:**
- Paper PF < 1.1 over 30 trades, or negative net.
- A single funding-trap event repeatedly trips the daily-loss halt.
- Fault-injection (§3) leaves state unrecovered.

If shelved: bank the discipline + the finding (edge is thin/scarce) as the win;
revisit only with more capital, a new instrument, or a new validated edge.

---

## 3. Pre-live hardening — required before the first live order

These are BUILT but never exercised against reality:
1. **Fault-injection dry run** (testnet or $50 mainnet): kill the process
   mid-position, inject a partial fill, drop the websocket → confirm reconcile
   rebuilds state and resumes reduce-only-then-clean.
2. **Restart-with-open-position** test: verify the account store restores the
   plan + stop after a hard restart.
3. **Funding-trap circuit**: cap consecutive losses within one funding event
   (the journal showed 4–6 stops before reversion) — halt re-entry after N.
4. **Halt-vs-stop reconciliation**: a $20 daily halt against ~$15 stops = ~1.3
   trades. Either widen the halt to ~3× the stop, or cut risk/trade so the halt
   is a real circuit, not a hair-trigger.
5. **Feed-continuity guard** ✅ BUILT (2026-08-10): a WS reconnect that skips
   closed bars, or a wedged loop, no longer silently poisons contiguous-index
   indicators. `_guard_candle_continuity` detects a time gap, REST-backfills it
   (deterministic heal), or fails closed to **reduce-only** (blocks new entries,
   keeps managing exits); a **stall detector** trips reduce-only when no bar
   arrives in >2.5× the timeframe. Counters (`gapped_candles`, `gap_fills`,
   `discontinuity_events`, `future_candles`) + `degraded` surface on the
   dashboard + journal. Still to exercise against a real reconnect before live.

### Residuals surfaced by the HLD review (2026-08-10)
- **Cross-venue timestamp invariant** — latent while all live lanes are
  binance-only; the guard now flags `future_candles` (a bar claiming to close in
  the future = skew/close-time-convention). Assert per-venue open-time before a
  second venue goes live.
- **Secret-logging scan** — one-time grep for accidental key/token logging
  (journal / alerts / dashboard) before the first live order.
- **Venue-quirk checklist** — Delta order types/settlement, Bybit account modes,
  Binance rate-limit/WS-outage behaviour: unexercised until the live adapter
  places real orders.

---

## 4. live_small parameters (only if GO)

- **Capital: $100–200 real** — well under the $1k point; this is a validation
  run, not a deployment. Prove the plumbing on money you can lose entirely.
- **Risk/trade ~1%** ($1–2). Sizing rounds DOWN; too-small is rejected, never inflated.
- **Daily-loss halt** reconciled per §3.
- **Kill switch armed** (`touch KILL`), reduce-only exits never blocked.
- **Monitoring wired for live-critical events** (feed dead, position stuck,
  drawdown breach) to Telegram — not just the read-only dashboard.
- Run `live_small` for **≥ 20 real events** before even discussing more capital.
