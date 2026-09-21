# Runtime capture and replay audit — 2026-09-17

## Result

Deployed `3ffa2bd1a0ee43151bf4b9d1614a02b0729afc81` through the sanctioned
scoped deployment script. Clean release suite: 3,351 passed, 6 skipped.
Five-lane capital audit: safe, no findings. No live permission changes.
Both recorders retained their start times and zero restart counts:
Delta September 17 10:27:24 UTC; public pulse September 14 13:46:24 UTC.
Dashboard returned HTTP 200 after the replacement; brief startup 502s occurred.

## Captured evidence

VM root: `/home/ubuntu/vnedge/logs/paper_trials/replay-inputs/`.

- BTC registration: `htf_regime_continuation_15m_v2__BTCUSD`, capture
  `aaf403a4f5634898af7e2c845688c816`.
- ETH registration: `htf_regime_continuation_15m_v2__ETHUSD`, capture
  `bada915f34c248fe8d62766fb9a08d05`.

Local unchanged copies: `/tmp/vnedge-runtime-evidence-SRPg1O/replay-inputs/`.
All six file digests were checked against capture manifests after transfer.
These are materialized startup inputs, not historical point-in-time proof.

For each symbol, 500 decision rows cover September 12 12:00 through September
17 16:45 UTC. 478 are canonical and hash-verified. The other 22 are explicitly
`exchange_ohlcv`, with unproven coverage. Both captures have the same affected
timestamps. They were not relabelled, dropped or converted to canonical bars.

Longest consecutive verified stretch: 84 bars. Latest stretch: 26 bars,
September 17 10:30–16:45 UTC. Registered strategy warmup is 224 bars; neither
stretch supports a strict replay even before a subsequent entry clock.

All 800 4h and 800 daily context rows passed the registered source policy for
each symbol. BTC: 76 canonical + 724 official 4h, 7 canonical + 793 official
daily. ETH: 75 + 725 4h, 6 + 794 daily. Admission is not proof that official
history was available in exactly this form at every historical decision.

## Actual replay attempts

Called `replay_bundle` separately for BTC and ETH using the exact captured
files and `registered_context_v1`. Both refused with
`15m: coverage_ok unproven`, before creating a replay output directory or order.
No historical fill, expectancy, edge or round-trip result is claimed.

## Independent runtime and repair evidence

Before this deployment, the 17:00 UTC runtime readiness check showed both
symbols data-ready and decision-ready, with identity and EMA200 context proven.
The BTC 17:00 evaluation nevertheless rejected: `regime_flat`,
`family_mismatch`, `structure_not_ready`, `one_hour_structure_unresolved`,
`side_alignment_missing`. Context was `mean_revert`, reason
`weekly_range_macd_off`. Structure reported six eligible hourly bars after the
11:00 quality reset and no confirmed swing pair. No ARM existed to execute.

The recorder repair report generated 16:50:39 UTC recorded zero inserted
parents, no replay errors, and unresolved 15m counts of 29 for each symbol
across its wider audited scope. This is not the same window as the 22 rows
above. Existing repair did not supply the absent evidence.

After deployment, the dashboard was reachable and both lanes data-ready;
decision readiness awaited the next closed-bar evaluation. Parity, execution
and live readiness remained false. Service health is not a claim of an edge.

## Remaining hard requirements

1. Independently verified historical trades for the affected intervals, or a
   sufficiently long newly captured uninterrupted window. Repair through the
   sole canonical writer; do not substitute exchange candles for decisions.
2. Repeat input admission and replay on that evidence; retain all failures.
3. A separately registered regime-appropriate hypothesis, if research is to
   address mean-reversion conditions. Do not retune the frozen continuation ID.
4. Validated expected-edge evidence: both pair contracts still have null
   `edge_model_id` and `oos_gross_edge_bps`. A setup cannot bypass CostGate.
5. A genuine qualifying paper path with clock, sizing, risk, persisted order,
   fills, fees, exits and reconciliation. Synthetic mechanics tests do not
   satisfy this acceptance criterion. Live capital remains off.
