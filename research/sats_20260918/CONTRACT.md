# SATS v1.13.1 — frozen exploratory test

Source: user attachment 8758437b-4d40-4863-9ffe-8acbfe069c37/pasted-text.txt.
Author attribution: WillyAlgoTrader. This is a Python research translation, not
a TradingView compilation or proven bar-for-bar Pine parity. No deployment.

Test BTCUSD/ETHUSD, 5m/15m, Auto and Crypto 24/7 presets (eight settings).
Auto5m = ATR10/mult1.5/ER14/SL1; Auto15m = ATR14/mult2/ER20/SL1.5;
Crypto = ATR14/mult2.8/ER20/SL2.5. Source=close; all adaptive, efficiency ATR,
TQI and character-flip defaults enabled. Fixed targets1/2/3R, thirds, stop-first,
timeout100 bars, pivot3/3 max age100, stop cap4ATR, min risk2 ticks.
Auto-calibration OFF, dynamic TP OFF, no grade filters, no parameter search.
Display scores, tables, labels, webhook formatting and learning statistics do
not control default trades and are not ported. No claim to test nondefaults.

ATR uses Wilder SMA seed and recurrence. Confirm pivots three bars later,
ties resolved to the latest equal extreme (rightmost). Tick-size assumptions:
BTC0.5USD, ETH0.05USD; these are research assumptions, not historical metadata.

Four separate execution worlds per setting:
1. source_close_gross: source-style close entry/flip/timeout, zero costs.
2. next_open_gross: queue entry/flip/timeout for next open, zero costs.
3. next_open_cost: same, fee5.9bps per fill and3bps adverse market slippage.
4. next_open_stress: fee5.9bps per fill and6bps market slippage.
TP limits retain their price (touch-fill assumption). All fractions pay fees;
stop gaps use worse(open,stop), no -1R loss clipping. Source close model skips
entry candle's extremes. Next-open model manages entry candle immediately.
For delayed entry freeze absolute SL/TP from confirmed signal, recompute initial
risk at actual fill, reject if SL breached/min-risk fails or TP1 already passed.
Pending flip exit settles before any new entry. Open trade with missing next
bar is censored, not carried across gaps. No new entry from gap-adjacent stale
pending evidence. Features restart after a gap; this differs intentionally
from a TradingView chart that compresses missing intervals.

Data: prior archive screen's hash-bound derived 5m data from17 supplied ZIPs,
113,131,407 trades. 15m needs three eligible5m children. Five observed minutes
is NOT completeness proof. UTC and contract sizes retain prior assumptions.
Funding/BBO/lot constraints/queue realism/risk gateway not modeled. No actual
orders or equity sizing. Results are modeled price return and initial-risk R,
not account PnL or a full VNEDGE paper replay. Promotion always disabled.

Evaluate fixed periods Jan–May, Jun–Jul, Aug, Sep1. Reset positions at each
boundary by censoring, not invented liquidation; features may use prior causal
history. No outcomes crossing periods. August was already examined by earlier
studies: historical test, NOT untouched OOS. Report all32 runs across4 periods,
including censored and rejected entries. Metrics: count, win fraction, meanR,
mean modeled net bps, PF in R, closed-trade additive R drawdown (not equity DD),
and exit-reason counts. No automatic winner, promotion, or tuning after results.
