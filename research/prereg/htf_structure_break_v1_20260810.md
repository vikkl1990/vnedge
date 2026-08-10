# PRE-REGISTRATION — htf_structure_break_v1

**Frozen: 2026-08-10.** No parameter, gate, or mechanical change after this date
counts toward promotion. Editing this file invalidates the judgment. This is a
research pre-registration, not a promotion or financial advice.

## 1. Identity
- Name: `htf_structure_break_v1`  ·  Version 1.0
- Decision timeframe: **1H** (primary), **4H** (bias)
- Markets (independent, never pooled): **BTC/USDT:USDT**, **ETH/USDT:USDT** on binanceusdm
- Class: purely mechanical price structure. **No order-flow, no L2, no funding, no ML.**

## 2. Economic thesis
Prior lanes (the 5m scalper/fee-wall family) died because the target was small
relative to round-trip cost — the edge was eaten by fees. This hypothesis flips
that: it accepts a setup **only** when the measured structural target is
**≥ 5× modelled round-trip cost**, and it expects **very low frequency**
(0–2 trades/day/market). Continuation only.

## 3. Frozen mechanics (the gaps in the brief, pinned — do not tune)
| Element | Frozen definition |
|---|---|
| **Swing detection** | Symmetric confirmed pivots via `liquidity_pools._pivots`, `left = right = R`, **R = 5** on the decision TF. Non-repainting: a pivot is known only `R` bars after it prints (availability lag = R). |
| **4H bias** | EMA(**20**) vs EMA(**50**) on **4H** closes. bias = +1 if EMA20>EMA50, −1 if EMA20<EMA50, **0 → no trade** (must be non-zero). |
| **1H structure event (BOS)** | With bias +1: a 1H **close** strictly above the most recent *confirmed* swing high → long continuation candidate. Bias −1: 1H close strictly below the most recent confirmed swing low → short. **CHOCH against bias is ignored in v1.** |
| **Target** | The nearest *confirmed* opposing swing level beyond entry (next confirmed swing high above for longs / swing low below for shorts). |
| **Hard target gate** | Reject the setup if `target_distance < 5 × round_trip_cost`. |
| **Stop** | Beyond the broken swing (the swing whose break triggered entry) + buffer **0.10 × ATR(14, 1H)**. |
| **Entry** | The **next 1H open** after the signal 1H candle closes. No intrabar/LTF refinement in v1. |
| **Vertical barrier** | **12 hours** (12 × 1H bars) → close at market. |
| **Concurrency** | **One** active observation per market. Exactly-once key: `htf_structure_break_v1:{symbol}:{side}:{1h_close_ts}`. |
| **Cost model** | Round-trip = taker both legs = **2 × 5 bps = 10 bps**, plus **2 bps** slippage/leg → modelled RT ≈ **14 bps**. The 5× gate ⇒ **target ≥ 70 bps**. |

## 4. Data
- Closed **1H and 4H** binance candles only for decisions. No future data (all
  indicators causal; pivots lag by R; 4H bias as-of merged onto 1H, backward).
- 1m/5m/15m explicitly **unused** in v1 (reserved for a future timing-refinement
  version, separately pre-registered).

## 5. Selection criteria — SEEN data (all must pass to proceed to the tail)
Walk-forward OOS on the **selection window** (see §7):
- ≥ **40** completed trades (aggregate across both markets)
- Gross expectancy > modelled cost
- Net expectancy > 0
- Profit factor ≥ **1.20**
- ≥ **1** market individually net-positive
- False-signal rate (stopped-out / total) < **65%**

## 6. Untouched gate — SEALED tail (stricter; judged ONCE)
- Net ≥ **+4 bps / trade**
- PF ≥ **1.30**
- Clean data quality (no gaps in the tail window)
- The sealed tail is opened **exactly once**; the verdict stands whatever it is.

## 7. Windows (declared before seeing any result)
- **Selection window (seen):** 2023-01-01 → 2025-06-30, walk-forward, both markets.
- **Sealed tail (untouched):** 2025-07-01 → 2026-06-30, opened once, only if §5 passes.
- If the sealed tail has insufficient history at run time, it is re-declared to
  the most recent 12 unused months and re-frozen here **before** the run.

## 8. Explicit non-claims
- No L2 / CVD / absorption. No funding. No meta-labeling until this scanner is
  itself net-positive on the sealed tail. A PASS here is a *candidate*, not a
  promotion — paper trading still requires separate human approval, and live
  capital still requires the full pre-live checklist.

## 9. Burn registry
- 2026-08-10 · selection window declared seen on first run · tail sealed, unopened.
- 2026-08-10 · **§5 VERDICT: FAILED — REJECTED.** Seen window 2023-01→2025-06,
  158 trades (BTC 84, ETH 74). Net **−$62.54** (BTC −$30.58, ETH −$31.96),
  **PF 0.73**, gross −$27.56 vs fees $34.98 (negative *before* costs), both
  markets negative. Only the trade-count and false-signal gates passed
  (false-signal 20% — the tight stop was fine; the directional edge simply
  isn't there). Exits: 86 TP / 40 max-hold / 32 stop.
  **DISPOSITION: shelved.** The mechanical HTF continuation, as frozen, has no
  edge on BTC/ETH 1H. The **sealed tail (2025-07→2026-06) stays UNOPENED** (§5
  did not pass) — preserved for a future, separately pre-registered hypothesis.
  The seen window is now burned for this hypothesis; no tuning-to-pass.
