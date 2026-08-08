# Code review — TradingView Pine indicators (2026-08-08)

Scope: `trinity_adx_pro_bias.pine` (© EMA34TRAD) and
`nadaraya_watson_liquidity_sweeps.pine` (© AlgoAlpha), both Pine v6, MPL-2.0.
These are chart-side research/visualization tools; nothing here feeds vnedge
execution. Review is read-only — no fixes applied to the scripts.

---

## Trinity ADX Pro Bias

### Bugs / correctness

1. **`trendDir` labels "Strong Uptrend/Downtrend" from the *trigger* level,
   and the real "Strong Trend" branch is unreachable.**
   ```
   trendDir = DIPlus > DIMinus and ADX > th1 ? "Strong Uptrend" :
              DIMinus > DIPlus and ADX > th1 ? "Strong Downtrend" :
              ADX > th2 ? "Strong Trend" : "Weak / Choppy"
   ```
   With defaults th1=10, th2=20: any ADX above 10 with a DI spread prints
   "Strong Up/Downtrend". The `ADX > th2` branch can only be reached when
   `DIPlus == DIMinus` exactly (or one is `na`), i.e. effectively never.
   This is visible on the attached ETH chart: Current ADX **15.24** — below
   the strong level 20, and with Slope Bias "Weakening Bear" — yet the table
   says **"Strong Downtrend"**. The table contradicts itself.
   Suggested fix: tier on th2 first, e.g.
   `ADX > th2 ? (bull ? "Strong Uptrend" : "Strong Downtrend") : ADX > th1 ?
   (bull ? "Uptrend" : "Downtrend") : "Weak / Choppy"`.

2. **Division by zero in `DX`.** `math.abs(DIPlus - DIMinus) / (DIPlus +
   DIMinus) * 100` — when both directional movements are zero over the
   smoothing window (flat/illiquid bars, or the very first bars), the sum is
   0 and DX goes `na`, which then propagates through `ta.rma` and can distort
   ADX and every alert built on it. Guard:
   `sum = DIPlus + DIMinus`, `DX = sum == 0 ? 0 : math.abs(DIPlus - DIMinus) / sum * 100`.

3. **"exact Wilder smoothing" is not seeded like Wilder.** The running-total
   form `S := S[1] - S[1]/len + x` is Wilder's *update* rule, but Wilder seeds
   the accumulator with the sum of the first `len` values; here it starts
   from 0 (first bar: `S = TR`). Early ADX values are biased low and take
   roughly 3×len bars to converge. Harmless on long charts, misleading on
   short history / replay from a fixed start. (The mix of running-total DI
   smoothing with `ta.rma` for ADX is fine — the `len` factor cancels in the
   DI ratio.)

4. **"Slope (5 bars)" is a 4-bar slope.** `slope5 = ADX - ADX[4]` spans 4
   bars. Use `ADX[5]` or rename the row. Same off-by-one framing for
   `slope2 = ADX - ADX[1]`, which is a 1-bar slope labeled "Slope (2 bars)".

### Design nits

5. **Asymmetric, undocumented slope-bias dead zone.** Thresholds are
   `> 0.2`, `> 0`, `< -0.3`: a bias in (−0.3, 0] reads "Neutral", and 0.2 /
   −0.3 asymmetry is a magic-number choice. Also these are absolute ADX
   points, so sensitivity varies with timeframe. Consider normalizing by ATR
   of ADX or exposing the thresholds as inputs.

6. **Table text color keys off `isBullishDI` only.** "Weakening Bull" prints
   lime and "Weakening Bear" prints red — the weakening state is invisible in
   the color. Cosmetic.

7. **Minor:** `var` on `slopeBiasDir` and `pos` is unnecessary (both are
   fully recomputed every bar; `switch` would read better for `pos`); the
   hidden "ADX Current" plot duplicates the visible ADX plot; alerts are fine
   but bug 2 can make the ADX-cross alerts flaky on symbols with dead bars.

### What's good

- The DM/TR construction itself is the standard Wilder definition and is
  causal (no lookahead, no `request.security` repaint vectors).
- Alert conditions use `[1]` comparisons for crossings — correct, fires once.
- Table redraw only on `barstate.islast` — cheap.

---

## Nadaraya-Watson Regression Liquidity Sweeps

### The most important property: it does NOT repaint

The classic NW-envelope indicators repaint because they re-fit the kernel
over the whole window and redraw the past. This implementation only ever uses
`src[i]` for `i ≥ 0` (past bars) with weights fixed per offset — it is a
causal, one-sided kernel (effectively a fixed-shape weighted MA with Gaussian
weights). Values do not change after bar close. The cost is endpoint lag,
which is why the oscillator is built on the *slope* instead. Good design.

### Bugs / correctness

1. **Division by zero in the oscillator.** `osc_ = nw_slope / slope_std` —
   if price is flat for `norm_len` (100) bars, `ta.stdev` returns 0 and the
   oscillator goes `na`, silently disabling fills, levels, and all alerts
   until variance returns. Guard with `slope_std == 0 ? 0 : ...`.

2. **"Sweep" detection uses `close`, not the wick.**
   `upper_level_swept = ... close > active_line_price`. A wick spiking
   through the level — the literal liquidity sweep the indicator is named
   for — does not register unless the bar *closes* through, at which point it
   is a breakout, not a sweep. If wick sweeps matter, test `high >` / `low <`
   (or track both wick-swept and close-through as distinct states/alerts).

3. **Only the latest level is monitored.** `active_line` is a single slot:
   when a new level is drawn, the previous one stays painted on the chart but
   its `*_swept` condition and alert can never fire again. Chart shows many
   red/green lines; only one is live. Worth knowing before trusting the
   "Level Swept" alerts — a stale drawn level is not a monitored level. Fixing
   it means arrays of (line, price, type).

4. **A new level silently abandons an un-swept active level.** In the
   maintenance block, `draw_new_line` alone sets `active_line_type := 0`,
   so an untouched level is retired the moment the oscillator produces the
   next signal cross — consequence of the single-slot design in (3).

5. **`bullish_rebound` compares across bars inconsistently:**
   `close[1] < nw_val and close > nw_val` uses *today's* curve value for both
   sides. The prior close should be compared to `nw_val[1]`. As written, a
   rising curve can print a "rebound" with no actual cross of the curve.
   Minor, affects only the ▲/▼ markers and their (absent) alerts.

### Design nits

6. **`math.pow(i, 2)` per iteration** in a 140-iteration loop per bar —
   `i * i` is cheaper; trivial either way at this size.
7. **`max_lines_count = 500`** — oldest lines are garbage-collected silently;
   fine, just know old levels vanish on long charts.
8. The two `fill()` calls both titled "Oscillator Fill" — the second (solid
   green/red zero-to-overflow band) overrides the gradient visually when
   |osc| > 2; likely intended, but the duplicate title makes the style
   settings confusing.

### What's good

- Causal kernel (no repaint), slope normalization by rolling stdev, and
  WMA/EMA smoothing are all standard and correctly ordered.
- Level anchoring to the phase extreme (`use_tip`) is correctly tracked with
  explicit state resets on phase entry.
- Alerts fire once per event (`crossover`/`crossunder`, and level type is
  reset after a sweep so sweep alerts can't re-fire).

---

## Relevance to vnedge

Both indicators are discretionary chart tools. If any of these signals are
ever ported into vnedge research lanes, the porting rules are the usual ones:
causality is already satisfied by both (good), but the Trinity table values
(slope bias, trend label) are last-bar display state — port the underlying
series, not the table logic, and fix bugs 1–2 (Trinity) and 1–2 (NW) first.
Any ported strategy still goes through the standard promotion machinery —
walk-forward gates, pre-registered judgment on untouched data, human
approval — like every other lane.
