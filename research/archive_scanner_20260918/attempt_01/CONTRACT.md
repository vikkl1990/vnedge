# Archive scanner screen v1 — frozen before replay

Research-only hypothesis screen. No registry, live configuration, gateway or
existing scanner changes. Historical evaluation is exploratory, NOT untouched
promotion evidence. All 17 user-supplied BTC/ETH 2026 archives are explicit inputs.

Archive timestamps are provisionally interpreted as UTC (unverified). Sizes are
contracts, provisionally converted with BTC 0.001 / ETH 0.01 base per contract.
No IDs or completeness manifest exist. Never deduplicate identical-looking
trades, fill empty minutes, or mark these bars canonical/performance eligible.
SHA256 binds each input. Validate symbol, positive finite values, integer size,
roles, ordered times. Reject malformed archives. Aggregate chunks to minutes,
then require all five observed minutes per 5m bar. Observed is not proven complete.

Features reset across absent/ineligible 5m bars. Warmup 50 consecutive bars.
EMA20/50, mean true range20, prior20 high/low and prior20 median base volume.
Regime: 20-bar efficiency ratio >=0.30 with EMA20 above/below EMA50 = up/down;
otherwise range. This is a 5m regime, NOT the deployed HTF telescope.

Three symmetric long/short detectors (ambiguous two-sided bars abstain):
- pullback: prior close above/below prior EMA20, current low/high touches EMA20,
  close recovers above/below EMA20 and prior close, directional candle.
- reclaim: wick breaches prior20 low/high, close reclaims it, directional candle.
- compression: prior20 high-low <=4 * prior ATR20, close breaks prior boundary,
  current volume >=1.5 * prior20 median volume.

Four variants: baseline; regime (pullback/compression aligned trend, reclaim
range); session; both. Session is one of four fixed UTC six-hour blocks. A
symbol/family/side/block is permitted ONLY if Jan–May baseline has >=30 measured
events, >=10 active days and mean gross return minus17.8bps >0. No retuning.

Measurement: decision at 5m close, entry NEXT 5m open, exit open three bars later
(15-minute hold). One event per family/variant/symbol at a time; opposite side
also waits. Reserve hold interval even if censored. Require all decision-through-
exit bars eligible. No outcomes spanning split boundaries. No SL/TP or account
sizing: this is directional-event research, NOT full paper-order replay.
Jan–May training; Jun–Jul validation; Aug historical holdout; Sep1 diagnostic.
Only training determines session permissions; validation/holdout cannot retune.

Costs: frozen research round-trip assumptions 11.8 /17.8 /23.8bps, not measured
fills or verified current fee schedules. Report gross and all sensitivities.
Funding, quote spread, queue position, latency, lot rounding and risk brakes
are not reconstructed. No net-of-all-costs or deployable-edge claim.
Metrics per symbol/family/variant/period: candidate count, overlap exclusions,
censor count, measured count, mean modeled net bps, PF, win fraction, additive
return-bps drawdown (NOT account drawdown), daily-block bootstrap 95% mean CI
(seed42,1000 draws; descriptive, not multiplicity-adjusted). All comparisons
reported, including zero events. No winner auto-selected or promoted.
