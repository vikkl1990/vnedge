# Preregistered offline paper-path probe v1 — 2026-09-15

Registration: `consolidation_scalp_5m_paper_probe_v1__BTCUSD`.
Claim: a closed consolidation breakout on BTC's canonical Delta 5m tape
may continue; this run tests its eligibility and paper routing, NOT profitability.
Parent research: `vwap_consolidation_5m_base_v1`; no live ID is modified.
Universe: Delta India BTCUSD only. Context: none. Entry: next 5m open.
`performance_eligible=false`, `can_trade=false`, `can_promote=false`.

Reuse the frozen generator and validated source loader from
`../vwap_consolidation_20260914/replay.py`, baseline only, without selection or tuning:
prior six-bar consolidation <=1.5 prior ATR20; close above prior high;
complete exact UTC-session coverage; common 30-minute episode reservation.
No VWAP/volume-filter selection based on prior returns.
Stop: prior range low minus 0.1 ATR. Target: decision close plus 2R.
Maximum hold: six 5m bars; single exit, no partial TP or trailing.
Evaluate on already-seen 2026-09-01 inclusive through 2026-09-11 exclusive.
This is exploratory plumbing, never untouched OOS.

Cost profile: frozen `delta_scalp_v2`, hash
`e04d112e35708157a77fc5b5721e4e2c4b2c0ce6d05c71b65bff3bf443e7b13a`.
Keep default CostGate margins. Fee 5.9bps/leg; total spread/impact
3bps/leg (1bp full spread plus 2.5bps adverse fill slippage).
Fill quotes are modeled from next-open OHLC, NOT recorded BBO.
No maker route or queue claim. Stop-first within-bar ties.
Funding absent: settled net remains unavailable; no promotion math.

No suitable edge-estimate artifact is bound to this new clock/path.
Therefore `expected_gross_edge_bps=null`; do not substitute target room,
reuse negative exploratory averages as validated estimates, or invent OOS.
Expected rejection is `edge_estimate_missing` before sizing or kernel submit.
Real-data rejection is a valid outcome; never weaken a gate to finish a route.
Separately, synthetic positive-edge unit fixtures test the full kernel,
risk gateway, order manager, simulated broker, reduce-only exit and reconciliation.
Their fills are not candidate evidence and must never enter the dashboard.

Each eligible episode is isolated with $500 paper equity and default frozen
risk limits, with exact contiguous closed 5m bars and source hashes retained.
Missing execution windows are censored, never filled or forward-carried.
Per-episode reports are NOT a portfolio backtest. Output directories are
write-once; bind contract, generator and source-file hashes before execution.
No network, credentials, global registry, roster, or production writes.
