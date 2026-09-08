# VNEDGE: scalper architecture audit

Audit date: 2026-09-08. Baseline: main `652b36d`. Production observation:
`vn-edge-sg-01`, `/state` at 18:15:52 UTC. This is a source/runtime audit, not
a profitability judgment, comprehensive security certification, or live approval.

## Verdict

The stated product is a scalper; the operational roster is a swing experiment.
Those are different contracts. The current BTC/ETH HTF continuation IDs should
not be weakened to produce scalp-frequency trades. Retain their history and
evaluate a separately registered scalp claim through the existing kernel.

`config/shadow-observers.v1.json` contains only the pair-scoped HTF continuation
V2 strategies: 15m structure, weekly/daily/4h permission, next-open entry, and
Delta swing costs. `src/vnedge/scalping/strategy.py` explicitly documents the
event-driven scalper interface as research scaffold with no production dispatcher.
Adding that interface did not turn the running bot into a scalper.

## What the VM actually showed

| Observation | BTC | ETH |
|---|---:|---:|
| Latest evaluated decision open | 18:00 UTC | 18:00 UTC |
| Daily observations | 800 | 800 |
| EMA200 ready / regime ready | true / true | true / true |
| Regime | mean_revert | mean_revert |
| Regime reason | weekly_range_macd_off | weekly_range_macd_off |
| Primary rejection | regime_flat | regime_flat |
| Structure ready | false | false |
| Decision-compute p95 | 1876.69 ms | 1888.87 ms |
| Close-to-arm p95 | 7576.66 ms | 9365.77 ms |
| Canonical wait p95 | 1256.72 ms | 1189.99 ms |

Both lanes evaluated the 18:00–18:15 bar, had hashed canonical decision rows,
and reported data/decision ready. Transport parity, kernel-envelope audit,
private-stream readiness and capital remained blocked. Counts are rolling
runtime observations, not a controlled latency benchmark; deployments can affect
their tails. The state also reports four exchange-OHLC rows in each working
window. The latest decision is canonical; this is not proof that every earlier
feature-window row is usable.

Immediately before that close, readiness exposed zero daily bars and missing
context while no fresh post-restart evaluation had populated the projection.
That did NOT mean 800 cached daily bars had disappeared. Do not diagnose warmup
from an empty startup projection alone.

The Binance repair process was retrying a canonical conflict. The current
`prerequisite_blocks_exchange` implementation already scopes that prerequisite
by venue; it is not evidence of a Binance prerequisite blocking Delta. A
missing post-restart Delta child/parent and an unrelated repair conflict must
be investigated separately. Do not overwrite published candles to clear either.

## Correctness fixes in this change

| Priority | Verified defect | Correction / regression proof |
|---|---|---|
| P0 | Invalid, delayed, future-skewed or out-of-order quotes returned without clearing PROBE. A later sample could complete a hold across broken evidence. | Reset only active probes; preserve arms, positions and last valid quote identity. Long and short regression cases prove a complete new hold is necessary. |
| P0 | A new arm under the same compression episode inherited samples from a previous bar/geometry/snapshot. | Exact rebind remains idempotent; changed arm clears PROBE samples without resetting episode budgets. |
| P0 | Missing ARM envelope after quote acceptance left `position_open` and the fire budget reserved although no trade existed. | Release the rejected reservation before reporting `decision_envelope_missing`; no acceptance or outcome is booked. |
| P0 | A verified fee prediction was transferable between maker/taker routes and venues based on symbol alone. This could lower the fee charged by CostGate. | Persist venue and entry/exit liquidity in the prediction. Require compatible route, Delta venue for Delta profiles, and a taker protective exit. Mismatched/legacy predictions fall back to the gate profile. |
| P1 | Research tick-stop simulation used a corrupt bid/ask before acceptance validation, potentially fabricating an exit. | Reject unusable top-of-book prices for simulated pricing and retain protection. A subsequent valid stop quote still exits immediately. No reduce-only gateway restriction was added. |

Files: `runtime/expansion_acceptance.py`, `runtime/squeeze_acceptance_observe.py`,
`risk/fee_model.py`, `risk/cost_gate.py` under `src/vnedge/`.

These are execution/evidence correctness patches. Detector parameters, swing
rules, roster, cost-profile vectors and capital settings are unchanged. Broken
quote sequences and misbound fee predictions can produce a different acceptance
set after the patch. Keep old replay artifacts attributed to their old commit;
rerun quote parity before comparing corrected acceptance with historical results.
Do not claim bit-identical execution on damaged data.

An initially suspected kernel strategy mismatch was ruled out: OrderIntent's
legacy `strategy_id` is a constructor-only InitVar, deliberately not persisted.
ExecutionEvidence validates its strategy and other identities against the ARM
envelope. Do not put strategy metadata back into the adapter instruction.

## Cost truth: estimates are not edge

The following are current repository defaults, not a fresh venue/account tariff
verification. They assume taker entry and exit, no funding payment and no verified
account offer. Spread/impact values remain modeled, not measured fill evidence.

| Default profile | Booked execution estimate | Safety reserve | `gate_cost_bps` display | Actual approval gross floor with default 4 bps minimum net |
|---|---:|---:|---:|---:|
| delta_swing | 15.8 | 3.0 | 18.8 | 19.8 |
| delta_scalp | 17.8 | 2.0 | 19.8 | 21.8 |

`CostGate.evaluate` approves expected net = gross edge minus booked execution
cost against `min_net_edge_bps`; its `gate_cost_bps` field is NOT that approval
threshold. A separate room multiple may impose another check. The current code
does not charge the display safety reserve again to booked PnL. Past descriptions
calling 18.8 the universal swing approval threshold are incomplete.

Remaining measured implementation inconsistency: for maker-entry/taker-exit
`delta_scalp`, CostGate uses 1.5 bps maker adverse selection plus 3 bps exit impact
(12.76 bps total). CostModel/SessionCosts retain 3+3 impact (14.26 bps total).
Taker calculations match. Do not pool these maker artifacts. Choose one
preregistered execution model/version and replay before promoting a maker lane;
this audit does not silently revise historical maker PnL or lower a gate.

The verified-account prediction route fix does not prove maker fills, actual
slippage, funding completeness, or account-offer eligibility. Lane BBO can test
taker touch/hold decisions; it cannot prove maker queue position. Visible L2
alone cannot guarantee passive fills either. Partial fills, cancels and venue
acks must be part of maker evidence.

## Remaining work, ordered for a scalper

1. **Freeze one scalp research contract.** Proposed starting shape: closed 5m
   geometry, distinct Delta BBO samples for entry, one frozen decision envelope,
   tick protection and an elapsed-time holding cap. The hold duration, max hold,
   and maker/taker route must be explicitly preregistered, not inferred from
   the swing roster or a fee-waiver window. Begin with one symbol for path proof;
   validate BTC and ETH independently before any broader claim.
2. **Record and replay the actual consumed quotes.** Carry event/receipt clocks,
   sequence, overflow and ARM identity. Exercise nonzero accept, reject,
   simultaneous-candidate, stale-book, restart and journal-failure cases through
   CostGate, sizing, gateway and kernel. No detached virtual fill is an operational
   fill. Tests of arithmetic are not production approval parity.
3. **Fix handoff latency before chasing indicator microseconds.** Keep Parquet
   authority until router/Parquet hashes AND arm/approval identities match on
   active cases. Attribute receipt, canonical wait, compute, journal and queue
   latency separately. Do not cut over simply to improve a displayed p95.
4. **Make exit ownership provable.** Runtime must register protection on the
   actual fill, handle partial fills and unknown submit outcomes, and maintain
   protection until closure is confirmed. The dormant
   `scalping/tick_stop.py` removes a stop when it emits an intent; it has no
   production caller and must not be wired into live as-is without pending-exit,
   retry/reconciliation and restart tests.
5. **Resolve the maker model split before testing maker economics.** Separate
   gate reserve, net approval threshold, booked estimates and actual fill fees.
   Partition artifacts by revision, symbol, entry clock, execution-cost model
   and fill assumption. Funding prints belong on held inventory, not on the
   entry clock. Do not assume an offer because a scalp intends to close quickly.
6. **Require an after-cost edge, then forward evidence.** Arena stays research
   only; it may generate candidates, not select the operational roster or unlock
   capital. Use untouched/OOS judgments with explicit multiple-testing tracking.
   A larger target or stop-distance multiple is geometry, not expected return.
7. **Show the actual product state on Desk.** Label current HTF lanes swing;
   distinguish startup/not-yet-evaluated context from insufficient history,
   playbook denial from stale data, and candidate acceptance from kernel fill.
   A healthy dashboard or research pipeline is not `live_ready`.

## Acceptance boundary

This audit fixes verified quote/evidence failures; it does not claim the entire
bot is now a profitable or production-ready scalper. Capital remains off. No new
strategy or roster is activated and no live order is sent. Deploying these fixes
requires the normal release process; the VM observation above describes the
baseline, not these local edits.

Tests: quote continuity on both sides; duplicate/rebind behavior; envelope
rejection unwind; corrupt-price protective simulation; verified fee route and
venue mismatch; normal valid discount behavior; existing kernel and cost tests.
Validation completed locally:

- `.venv/bin/python -m pytest -q`: **2,802 passed, 6 skipped**, 168.68 seconds.
- Changed-file Ruff check: passed.
- `git diff --check`: passed.
- Existing dependency deprecation warnings remain (611); they did not fail tests.

These edits are uncommitted and undeployed. The full suite is not a substitute
for lane-consumed quote parity or production exit/reconciliation evidence.
