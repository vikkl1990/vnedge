# Structure, queue and paper-path audit — 2026-09-15

## Production findings (read-only inspection)

Serving revision inspected: `7786ab461a18b1b8f5a00f7c7915cae301a14a8d`.
No live scanner parameters, permissions, roster, cost profiles or market rows changed.

Both HTF lanes' evaluated 224-row windows ended at 2026-09-15 01:15 UTC:
215 canonical rows and nine exchange-cache placeholders, marked `gap`.
Exact `CandleParquetStore.get_bar` returned missing for all nine 15m opens:

| UTC date | Missing 15m opens, BTC and ETH |
| --- | --- |
| September 12 | 18:45, 21:00 |
| September 13 | 18:15, 18:30, 21:00, 21:30, 21:45 |
| September 14 | 05:30, 13:45 |

This invalidates six hourly parents in each window: September 12 18:00/21:00,
September 13 18:00/21:00, September 14 05:00/13:00.
The last reset is **14:00 UTC September 14**, not a reset every evaluation.
At the 01:30 decision close there were eleven eligible closed hourly parents
since that reset, one confirmed high and zero confirmed lows.
The 3/3 confirmation rule correctly cannot declare a swing pair yet.

For both symbols, the 05:30 and 13:45 15m buckets each contain fourteen good
1m children plus one `partial, coverage_ok=false` child: respectively 05:40
and 13:46. The complete-child ladder correctly refuses those parents.
Both recorder containers started at **2026-09-14 13:46:24 UTC**, with restart
count zero. This directly ties the latest partial minute to container
replacement/startup, not a scanner threshold or recurrent process crash.
The earlier partial minute's operational cause was not independently established.

Do not change these historical rows to `ok` or fill with official OHLC.
Repair requires complete verified raw-trade coverage and a disclosed new hash;
this audit has not established that such missing prints can be recovered.
Preserve the running canonical owners during unrelated UI/research rollouts.
The sanctioned scoped deploy can restart only multi-lane-shadow/dashboard-tls.
Repeated full-image fleet recreation risks introducing another partial minute.

There is a separate permission denial: both last completed HTF evaluations
reported `regime_flat`, daily/4h MACD impulse-fade and weekly range. Each had
800 daily observations and EMA200 ready. Warmup is not the immediate blocker.
Fixing structure does not imply continuation permission or an edge.
The three measurement lanes never create OrderIntents; HTF lanes are
SHADOW_OBSERVE, not authorized paper execution lanes.

## Queue repair

The persisted HTTP-driven queue index was about 8.65 MB behind per source:
its newest event was September 14 07:45, while journals had September 15
01:30 evaluations. Previously each request advanced at most 512 KiB.

The disposable recent-window index now jumps to a bounded latest tail when
backlog exceeds its read budget. It clears old indexed ancestors, preserves
the journals, persists skipped-byte coverage and discloses that limitation in
the UI. Subsequent caught-up reads retain the disclosure. A fill without its
retained intent/submission remains an incomplete chain, never a proven fill.
An index-current label means caught up to the read journal, not healthy
scanners, complete history or profitability.

## Registered paper probe

Contract: `research/reports/paper_probe_20260915/CONTRACT.md`.
ID: `consolidation_scalp_5m_paper_probe_v1__BTCUSD` (offline research only).
Artifacts: `research/reports/paper_probe_20260915/attempt_02/`.
Attempt 01 is retained; attempt 02 verifies identical results after provenance
metadata completion/import cleanup. No strategy, sample, gate or threshold changed.

Frozen baseline generator reused, no winning-variant selection. Already-seen
canonical Delta BTC data, September 1–10 inclusive; closed 5m decisions,
next-open modeled taker execution; `delta_scalp_v2` unchanged.

**29 episodes; 29 canonical ARM identities; 29 edge_estimate_missing
rejections; zero orders and zero fills. No execution-window censorship.**

This did NOT demonstrate a full real-candidate round trip. No suitable bound
edge-estimate artifact exists, so the cost boundary stops it before sizing.
Target room was not substituted for expectancy. Settled funding is unavailable;
settled net remains null. Zero paper PnL is inactivity, not a breakeven edge.

The old offline PaperRunner recorded CostGate as not evaluated. New explicitly
gated replays can now evaluate the unchanged CostGate before sizing and attach
its exact result to the kernel evidence. Legacy replay behavior is unchanged.
Strict canonical identity is separately enabled for this new contract.

Synthetic unit fixtures (not market evidence) prove the downstream complete
paper path: approved cost → risk gateway → kernel/order manager → simulated
entry fill → reduce-only exit → flat portfolio/clean reconciliation.
Negative/missing edge and missing canonical identity remain rejected.
No synthetic fills were sent to production or the dashboard.

## Remaining boundary

Before a real candidate can traverse the full paper route, it needs an honest
bound edge-estimate artifact under its exact strategy/symbol/clock/cost contract.
No promotion, new live permission, invented OOS or lower wall is implied.
Deployment scope is multi-lane-shadow and dashboard-tls only; keep both
recorder containers unchanged and verify queue offsets against the live journals.

Validation: full suite 3,273 passed / six skipped; 47 targeted regression
tests (including the additional repeated-reset regression) passed; 79 frontend
tests passed and production frontend built. Both attempts' journal hash chains
verify cleanly. Attempt 02 binds the final replay runner and harness hashes.
