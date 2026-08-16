# Edge Investigation Post-Mortem — 2026-08-16

**Verdict:** Every fast-timescale edge candidate was investigated through the promotion
machinery. All were killed; none reached live capital. The cause is not a strategy flaw
but two structural cost walls a <$1k retail account at these venues cannot cross.

This document is the durable record. It is referenced by `strategy_registry.py`
(`KILLED`), `hf_engine_registry.py` (`KILLED_HF_ENGINES`), and `signal_engine.py`.

---

## The two walls (per-trade taxes)

| Wall | Applies to | The inequality |
|------|-----------|----------------|
| **Taker cost** | directional strategies | edge ≈ 0.3–2 bps  **<**  cost ≈ 8 bps (≈5 fee + ≈3 spread/slip) |
| **Adverse selection** | passive market-making | ½-spread ≤ 0.71 bps  **<**  immediate adverse move ≈ 0.5–1 bps, before ≈2 bps/side maker fee |

Every candidate below is a different attempt to find a per-trade edge larger than one
of these taxes. None did. Raw edges were frequently *real* — that was never the problem.

## Kill ledger

| # | Hypothesis | Class | Raw edge? | The killing number |
|---|-----------|-------|-----------|--------------------|
| 1 | funding mean-reversion (BTC) | directional/taker | 3× OOS-positive backtests | forward paper **−$16.60**, DD 7.35% > 6% cap, 3W/5L |
| 2 | crypto-trend (DOGE) | trend/taker | paper +$16.15 | only **3 trades** — immature, shelved (not killed) |
| 3 | order-flow / CVD (900 hyp) | directional/taker | +0.32 bps median, 93% + | net **−7.68 bps**, **0/900** positive after cost |
| 4 | HF order-flow imbalance (1s) | directional/taker | ~0 gross drift | **0/6901** survive; realized −13.9 bps ≈ cost = diffusion |
| 5 | HF mean-reversion (1s) | directional/taker | — | fired **0×** on real data (gates never align) |
| 6 | profit-ladder / horizon map | directional | ~0 at every horizon | no net drift **3 s → 60 m** |
| 7 | hourly-range breakout + trail | directional/taker | +8.8 bps/5min (ETH slice) | OOS **−4.78** (seen +3.76); oracle +9.89 unharvestable; drift 10.8→2.5 |
| 8 | passive market-making | maker | ½-spread ≤ 0.71 bps | markout **< 0 @ 0.5 s**, all 5 symbols, before fees; no reversion |

**The pattern:** at fast timescales the per-trade edge is always ≈ or < the per-trade
cost of capturing it. Speed is structurally the wrong direction for a small account here.

Row 7 detail (the most promising, hence the most instructive): pre-registered OOS test,
28 days × ETH/BTC/SOL, 155 signals, honest 16 bps cost (incl. exit slippage). The
time-armed exit reproduced on the tuned slice (+3.76) then collapsed out-of-sample
(−4.78) over 124 untouched trades — a textbook IS/OOS collapse. An oracle exit (exit at
the exact MFE peak, perfect foresight) was +9.89 OOS, but no *causal* exit captures it:
the ~15 bps gap is the no-foresight tax. The drift is real but smaller than the
volatility it is buried in.

Row 8 detail: for every trade, treat ourselves as the resting maker that got hit;
signed maker PnL vs future mid starts at +½-spread and decays via adverse selection. On
all five liquid symbols it is negative by 0.5 s and flat-to-worse to 60 s — the adverse
move is permanent (informed), so there is no reversion to wait out. This is the
optimistic fill case (top-of-book, every touch) and before fees.

---

## What IS the product

The negative alpha result was produced by a positive asset: a production-grade safety
and validation system. Its demonstrated value is every deployment it stopped.

- Single non-bypassable `PreTradeRiskGateway`
- Journal-before-submit WAL + hash-chained fill ledger
- Mint-once idempotency; `TIMEOUT_UNKNOWN` blocks new risk
- Fail-closed reconciliation (rebuild from exchange)
- File-based kill switch (never auto-resets) + daily-loss halt
- Three-gate live confirmation + mode ladder (backtest → paper → shadow → live_small → live_full)
- `CostGate` hard fee-wall filter
- Pre-registration + purged walk-forward OOS promotion machinery — the thing that killed rows 1–8

**Keep all of it.** The discipline turned eight tempting candidates into seven clean
kills before any touched live capital.

---

## Enforced policy (how this is kept true in code)

"Delete permission to trade, not the measurement code." The engines and strategies stay
importable for research and backtesting; only capital permission is removed.

- **`strategy_registry.py` → `KILLED`**: `funding_mean_reversion_v1` is no longer
  `is_capital_eligible` — it downgrades to a SHADOW lane (`multi_lane.capital_downgraded`)
  and can never back a paper/live lane. Re-enable only with new structural evidence on
  untouched data through the ladder.
- **`hf_engine_registry.py`**: all five HF engines are in `KILLED_HF_ENGINES` with their
  post-mortems; `TRADEABLE_HF_ENGINES` is **empty** (fail-closed). No `SignalEngine` is
  `tradeable`. Passive MM is recorded (`PASSIVE_MM_POSTMORTEM`) though it was only ever a
  research script.
- **`signal_engine.py`**: `SignalEngine.tradeable = False` by default (measurement-by-
  default); `edge_estimate_bps` means a conservative measured move, never a heuristic.
- **`tests/test_edge_kill_policy.py`**: a tripwire fails if a kill is undone or a new
  `SignalEngine` subclass appears without a recorded kill/promotion decision.

### Do NOT
- Re-enable a killed engine/strategy without a pre-registered OOS pass on untouched data.
- Add a new 1 s / hourly directional signal engine expecting a small edge to net out.
- Build a `PassiveMMEngine` expecting the spread to be free income.
- "Improve the exit until IS looks green" — it rearranges noise and dies OOS (row 7).

---

## The one door not yet closed

The cost wall is a per-trade tax. A scalp fighting 8 bps against a 5 bps move is
hopeless; a **swing / position** trade paying the same 8 bps against a 150–300 bps move
barely notices it. That class reuses the *existing* backtester, walk-forward, and
promotion machinery (not the HF tick stack), under the same pre-registration discipline.
`crypto_trend_atr_margin_v1` stays capital-eligible as the swing-adjacent candidate.

This is a hypothesis, not a promise — it can fail promotion like the rest. But it is the
only class the two measured walls do not structurally kill. No live until OOS passes.

*Nothing herein is financial advice.*
