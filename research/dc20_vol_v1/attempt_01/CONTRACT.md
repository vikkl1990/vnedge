# DC20 volatility variants — exploratory, frozen before results

Independent books for off, atr_exp, adr_band, skip_comp and atr_mid. Same BTCUSD
daily venue OHLC, January 1 2025 through September 19 2026 as the prior DC20
replay; November 2024 warmup. No protected BTC judgment dates are opened.
Also replay independent flat-start 90-day and 14-day windows, not slices of a
continuous book. These are already-seen exploratory data, not untouched OOS.

ATR14/ATR50 use rolling SMA of true range, NOT Wilder. ADR uses rolling SMA of
high-low. Current completed bar is included; range remains prior 20 days.
TR requires a valid previous close. Incomplete/missing bars are masked; no
compressed calendar. Missing/zero/nonfinite volatility denominator => SKIP_VOL
for a gated variant, never pass. Off has no volatility warmup requirement.

- off: unchanged baseline, no volatility filter.
- atr_exp: ATR14/ATR50 >= 1.0 inclusive.
- adr_band: .75 <= ADR14/ADR50 <= 1.40 inclusive.
- skip_comp: ADR14/ADR50 >= .75 inclusive.
- atr_mid: .85 <= ATR14/ATR50 <= 1.30 inclusive; separately labeled comparison.

Gate evaluated only while flat, after range validation, before directional
breakout/risk checks, matching the supplied step function. Management precedes
the gate, stop first, no kill/trail, no same-bar reentry. A failed volatility
gate never exits an existing position. Each variant replays its own occupancy,
not a post-hoc filtered trade list. Entries keyed by close timestamp plus side
are compared to baseline; no shared position lock or ST4H dependency.

Fixed $1000 entry notional; .00059 fee each side; $.50/BTC cash slippage each
side; no funding, lot rounding, margin, gateway or MTM DD simulation. Close fills
and gap-stop fills are literal optimistic assumptions. Final open positions are
reported, excluded from closed metrics. Gaps while holding halt that book as
unresolved. Missing data never imply flat. Same limitations as DC20 baseline.

Report every mode/window, actual baseline entry ratios, added/removed entry
identities, changed closed-trade outcomes and final positions. Validate off
against the existing saved baseline (trades, metrics, inventory).

"Skip 10% widest days" is NOT tested: no width definition, reference population,
or causal percentile lookback was supplied. Do not invent a full-sample quantile.
No promotion, runtime default change, live sizing, deployment or activation.
