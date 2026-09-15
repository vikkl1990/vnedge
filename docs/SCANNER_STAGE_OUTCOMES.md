# Scanner stage outcomes v1

This optional, report-only research product tests the **data binding**, not a
trading hypothesis. It records Analyst context beside future scanner evaluations.
It changes no scanner ID, signal, gate, cost, decision hash, roster or permission.

## Contract

- First worker activation is persisted. Earlier decision-bar closes are excluded.
- One write-once binding per exact journal evaluation or `(lane, decision_id)`.
  Diagnostic evaluations have no decision ID and are never completed trades.
- Context cutoff is the decision-bar close, including quote-hold decisions.
  Both the official OHLC snapshot and its stage report must have been received
  by that cutoff. An old candle downloaded later is not then-available evidence.
- Stage computation is not rerun on today's history. The last eligible saved
  4h/1d report is bound with its source, receipt, report ID and specification hash.
  Missing, stale, changed-source/pending and unknown contexts remain visible.
- Source is `official_delta_ohlc`, **not** canonical trade-lake identity.
- A sidecar polls journals, so the capture timestamp may follow entry. It does
  not backdate that capture; eligibility comes from immutable source/report
  availability at the cutoff. This is an observational join, not proof the live
  scanner used the Analyst feature. No ML pre-entry feature contract is replaced.
- Order metadata may come from a preceding evaluation of the exact same lane,
  strategy, symbol, timeframe, clock and decision close. No fuzzy time joins,
  filename-derived exchange, later fills or cross-symbol metadata.

## Outcomes and comparison

Only `build_ledger_labels` supplies results: verified paper journal and fill hash
chains, exact ARM envelope, flat-to-flat accounting, fees, settled funding and
clean accounting checkpoints. The binding and ledger must agree on identity.
Duplicate decision outcomes or contradictory identity invalidate the lane's
comparison. Broken current ledgers cannot reuse an old successful summary.

Virtual shadow PnL, unbound exits and diagnostic evaluations remain unscored.
Funding is **included** in this verified-paper ledger view and explicitly labelled;
do not mix it with funding-excluded research/promotion books or subtract costs again.

Cohorts partition by lane, strategy, exchange, symbol, timeframe, clock, mode,
cost profile and cost hash, label contract, and stage source/specification. Each
cohort shows all bound outcomes plus independent 4h and 1d breakdowns. Do not sum
those breakdowns. Unknown/missing context is retained as its own bucket.

Mean net bps and booked USD describe the sample. Below 30 outcomes the status is
`INSUFFICIENT` and PF is withheld. Larger samples are `DESCRIPTIVE_ONLY`, not an
edge verdict. This does not calculate portfolio drawdown from an invented equity
curve, control multiple comparisons, or constitute untouched OOS validation.
A stage-filtered scanner still needs a new strategy ID and preregistered replay.

## Operations

`analyst-stage-outcomes` is opt-in under the `analyst` Compose profile. It has no
network, credentials, execution adapter or write access to journals/history.
Its sole-writer lock and own evidence/index directory are independent of the
scanner. The dashboard mounts its evidence directory read-only.

Before deployment, provision `data/analyst_stage` for the deployment UID/GID;
do not let Docker create a root-owned bind directory. Deploy only committed
code through `scripts/deploy.sh`, including `analyst-stage-outcomes` and
`multi-lane-shadow` in the scoped services list. This document does not authorize
deployment or enable trading.

The worker polls every 60 seconds. Health means a report younger than 15 minutes,
not adequate labels or verified market readiness. Empty ledgers, index lag,
malformed records, missing directories and processing limits are exposed separately.
The index is a bounded recent journal window, not a complete scanner archive.
Bindings/outcomes are append-only. Storage exhaustion fails visibly; archive
evidence before the AnalystStore 1 GB limit. Accounting verification is bounded
by the existing ledger verifier and CPU/memory limits, outside HTTP and trading.

API: `/api/crypto-analyst/stage-outcomes/BTCUSD?exchange=delta_india`.
UI: Analyst → symbol → **Scanner outcomes**. No mutation endpoint.
