# VNEDGE Signal Queue + ML Lab

Status: proposed architecture, not an implemented or approved trading change.
Date: 2026-09-13. Scope: extend the existing React cockpit and Arena.
The supplied screenshots are product references, not performance evidence.

## 1. Product contract

Two connected surfaces, one evidence chain:

- Desk / Signal Queue answers what the scanners evaluated, what actually armed,
  what was denied, and what reached an order or an outcome.
- Arena / ML Lab answers whether frozen mechanisms and models generalize after
  declared execution costs. It never issues orders or promotes itself.

Do not build a second scanner, venue client, candle store, or execution book.
Do not expand the universe or reactivate rejected strategies to populate tables.
Current BTC/ETH scope remains; new universe/clock/claim changes require contracts.

## 2. What to adopt and reject from the reference

Adopt the dense queue, side chips, visible reject reasons, expandable evidence,
training progress, calibration, walk-forward and feedback views.

Do not reproduce:

- `CONF 56` or letter grades without a frozen scoring definition. A heuristic
  score is not a probability and cannot cancel a hard rejection.
- `ML 69%` without model, target, horizon, calibration and prediction timestamps.
- `closed +$0.29` on an unfilled candidate or simulated bar-touch observation.
- `VALID EDGE` based only on a positive point estimate or a handful of outcomes.
- `COMPLETE` meaning both a successful job and valid/fresh research evidence.
- One best/worst ranking combining unrelated markets, clocks and cost profiles.
- Public Train buttons: the dashboard has public read-only viewers.

## 3. Decision chain and identities

Keep the existing DecisionEnvelope and ExecutionEvidence outside OrderIntent.
decision_id remains minted at ARM from strategy/version, symbol/TF, closed bar
content hash, side, snapshot_id and entry clock. No quotes, path or random venue
identifier enter that hash. Mint the random client_order_id once after risk
passes; journal it before submission and reuse it verbatim on retry.

Before ARM, a rejected evaluation has a diagnostic record identity, NOT a
decision_id. Call it evaluation_id; namespace it as diagnostics, stable over the
same recorded evaluation inputs. If input identity is absent, use the source
record locator and mark evidence incomplete. Never invent canonical proof.
Use a namespaced display row_key to unify navigation, not to confer authority.

The queue projects evaluation -> arm -> accept (quote path only) -> cost/size/risk
-> submit -> ack/fill -> resolve. This is a projection of existing record kinds,
not a mandate to rename the WAL or event-source the whole bot.

Rejection, expiry, partial fill, cancel, timeout-unknown and reconciliation are
explicit states. ACK is not FILL. Multiple orders/fills may descend from a
decision; aggregate them with their exact linkage, not nearest timestamp.
An identity conflict is quarantined and visible, never last-write-wins.

## 4. Data contracts

QueueRow (read model):

- row_key, evaluation_id?, decision_id?, snapshot_id?, schema_version;
- strategy_id, strategy_revision/source_hash, exchange, symbol, timeframe;
- entry_clock, execution_mode, outcome_basis, cost_profile_id/config_hash;
- decision_open/close, observed_at, last_event_at; timestamps are UTC;
- side?, proposed_entry/stop/target?, actual_entry?, filled_quantity?;
- lifecycle_state, primary_failed_gate, all_failed_gates;
- feature_snapshot_id?, score_spec_id?, model_prediction_id?;
- managed_order_refs, outcome_refs, provenance_state, projection_cursor.

Absent values remain null. A denied regime evaluation has no fictional entry
or stop. Row time means decision close by default; receipt and fill times are
separate fields. Vela markers still use decision-bar OPEN.

FeatureSnapshot:

- decision_id (or research evaluation identity before ARM), bar hash, snapshot_id;
- ordered feature schema and calculation hash, feature values, missingness;
- exact exchange/symbol/TF, feature_as_of and persisted_at, input artifact hashes.

PredictionEvidence:

- model_id/hash, feature_snapshot_id/hash, target_spec_id, calibration_id;
- predicted_at, feature_as_of, raw output, calibrated probability or null;
- training/calibration cutoff, coverage status, validity, report_only=true.

OutcomeEvidence:

- decision/order/fill links; entry/exit timestamps and execution policy;
- gross PnL, fees, spread/slippage accounting, funding, net PnL;
- initial risk denominator, net R, net bps, holding time and resolution status;
- outcome_basis = historical_simulation | counterfactual | paper_kernel |
  shadow_kernel | live_venue; fill_assumption and coverage requirements.

Define R from the frozen initial monetary risk, not a changed trailing stop or
leverage. Record each cost once: fill prices may already contain spread/slippage.
Unknown funding is null/excluded, not zero. Gate reserve is not a booked fee.

## 5. Storage and read API

Keep decision WAL, fill ledger, feature logs, lake and research artifacts as
their existing durable sources. Extend a rebuildable SQLite projection beside
the existing evidence index; do not make SQLite the order authority.

A single projector worker owns writes. Persist source generation/record cursor
and projection updates in one transaction. Detect WAL rotation/truncation and
hash conflicts. Restart can safely replay records; duplicates cannot increase
fill counts, labels or PnL. Use indexed queries and cursor pagination, not full
journal scans in HTTP handlers. Full verification remains offline.

Proposed APIs (reuse current journal/research handlers and auth):

- GET /api/signal-queue: cursor + strategy/market/TF/clock/mode/state filters.
- GET /api/signal-queue/{row_key}: full timeline and evidence references.
- GET /api/ml-lab: compatible-cohort summary, counts, freshness and failures.
- GET /api/ml-models/{model_id}: provenance, calibration and validation artifacts.
- GET /api/research-jobs/{job_id}: stage, heartbeat, coverage and artifact refs.
- Operator-only research enqueue/cancel: use existing governed job machinery;
  server-side role checks, CSRF, idempotency, quotas and immutable request hash.
  Canceling a research job must not cancel any trading order.

WebSocket notifications carry cursor/version invalidations; REST is the recovery
path. Network reconnect refetches, it never resubmits a mutation automatically.
Each response carries source_as_of, projected_at, last_event_at and stale state.
A recent HTTP response cannot make an old source artifact fresh.

## 6. Desk UX

Signal Queue replaces neither Monitor nor Book. Default columns: time, market,
side, strategy revision, clock, planned entry/stop, stage, main reason, ML status
and outcome basis. Additional details expand rather than making the row unreadable.

Tabs: Armed decisions; All evaluations; Orders/fills; Research observations.
Default the actionable table to real arms, with evaluation counters alongside.
No arms is a valid empty state. Show last evaluation, data coverage and dominant
denial instead of generating consolation signals.

Header counts must distinguish evaluated, armed, accepted, submitted, filled and
resolved. `Last 10 / 3 filled` uses the same displayed cohort and time window.
Blocked rows display all rejects on expansion; grade and reject are different
columns. ML shows `collecting`, `unavailable`, `uncalibrated` or a qualified value.

Click row: decision/context -> feature snapshot -> quote samples -> cost/risk ->
orders/fills -> realized costs/outcome. Every card points to the underlying record.
Simulated results are visibly labeled and excluded from operational Book totals.

Separate chips for SERVICE, EVALUATION, SETUP and EXECUTION PERMISSION. Before
the first current-process evaluation, show awaiting/null; do not borrow a prior
session's `ready=true`. Soft latency warnings remain visible in Ops.

## 7. ML Lab and research flow

Reuse Arena with views: Overview, Datasets, Scanner Results, Backtests,
Calibration, Features, Walk-Forward, Models and Forward Feedback.

Continuous bounded workflow:

1. Freeze mechanism/universe/clock/cost, feature/label definitions, data snapshot,
   search budget and train/calibration/validation windows before execution.
2. Run coverage, closed-bar, identity and causality preflight. Missing BBO for
   quote-hold or depth/queue evidence for maker assumptions blocks those tests.
3. Run the unchanged rule-based baseline and retain failures.
4. Resolve mature labels; pending, censored and ambiguous outcomes remain visible.
5. Train candidate models only within their declared population and resource budget.
6. Calibrate on a separate chronological calibration fold; test on later unseen
   folds. Never fit the calibrator on the evaluated test predictions.
7. Compare baseline vs model on identical eligible opportunities, costs and risk
   policy, then write immutable results and a rejection/candidate verdict.
8. Any untouched judgment or operational promotion remains human-gated. Automatic
   candidate generation, training or a completed run grants no trading permission.

The first ML role is report-only meta-labeling: probability of a declared signal
outcome, not free-form direction. Do not silently redefine net-positive labels
as TP-before-SL; those are different targets, especially with timeout exits.
Model inference that affects decisions would require a new version and causal
feature availability before that decision. Background post-decision inference
is analysis only and must not be presented as a historical live prediction.

Use the existing 200-label training floor, but do not advertise 200 as validated.
The current harness has a 300-label CPCV entry floor and still requires each
usable training fold to clear 200 rows. Derive UI readiness from actual checks.
Purge intersecting label intervals using event times and full label horizon;
do not assume one sparse signal row equals one bar. Group overlapping episodes
and test temporal/market generalization; no random splitting of adjacent trades.

Keep registered robustness gates. Report AUC/PR metrics, Brier/log loss,
calibration bins/counts, sample coverage, after-cost expectancy, PF, drawdown,
tail risk, trade count and uncertainty. Classifier accuracy is not an edge gate.
Track all attempted variants; repeatedly inspecting a holdout burns it.

ML outcome populations must remain distinct:

- Executed kernel outcomes support operational model evidence only with valid
  envelope/order/fill linkage and original available-at-decision feature snapshots.
- Rejected-setup counterfactuals and historical simulations may support declared
  research datasets, never masquerade as fills or satisfy the operational floor.
- Training only on accepted fills creates selection bias. Retain eligible rejected
  opportunities for controlled baseline comparisons, with simulated labels marked.

## 8. Ranking and score semantics

Partition results by strategy revision, exchange, symbol, TF, entry clock,
cost profile, fill assumption, mode, dataset and evaluation window. Cross-cohort
views show separate panels, not pooled headline PnL. Portfolio totals are a
separate reconciled account view, not a strategy ranking.

Evidence states: insufficient -> exploratory -> validation pass/fail -> untouched
judgment -> forward evidence -> separately approved eligibility. No unexplained
`VALID EDGE` badge. Report estimate uncertainty and independence, not just n.
Positive expectancy can still fail drawdown, concentration, coverage or parity.

If a grade is desired, expose a frozen report-only score_spec and contributions;
label it setup score, not confidence. Hard gates always win. Any score threshold
that changes which signals fire is a new strategy version and experiment.

## 9. Runtime isolation and availability

Reuse existing research/job workers; train outside the scanner event loop and
without venue credentials. Apply CPU/RAM/concurrency limits, especially on this
shared VM. Start with one heavy training job at a time. Pause research work on
resource pressure, never loosen runtime latency or entry gates.

Each worker publishes a process heartbeat separately from last completed artifact.
Job states: queued, data_blocked, running, succeeded, failed, canceled. Artifact
status: fresh/stale. Validation verdict: pass/fail/insufficient. Keep these separate.
Persist leases/checkpoints; restart resumes or records interruption without
duplicate attempts. The watchdog repairs bounded process failures, not market
denial or lack of research samples. External host-down monitoring remains needed.

## 10. Current code findings and extension points

- execution/evidence.py: DecisionEnvelope already verifies ARM hash/snapshot/side.
- dashboard/app.py /trade-journal: existing read-only journal/fill projection.
- research/evidence_store.py: existing JSON + optional SQLite research index.
- research/continuous_ai_pipeline.py: bounded, result-independent candidates and
  immutable contracts; extend, do not add an unbounded generator beside it.
- ml/feature_log.py: background snapshot feature logging, weaker durability than
  the order journal by design. Missing training rows must remain visible.
- ml/meta_label_dataset.py: strict build_meta_label_dataset_from_log exists.
  Its older builder derives features by entry timestamp and falls back by symbol.
  For next-open entries, entry-bar close features risk using information not yet
  known at entry. Require decision-time availability, not timestamp coincidence.
- research/ml_pipeline_status.py currently calls that older candle builder, not
  the strict feature-log join. Wire the strict path for operational training.
- ml/meta_labeler.py describes calibration, but its evaluated path calls classifier
  probabilities directly and computes ECE. Calibration plumbing/artifacts must be
  verified/implemented before displaying calibrated probability in the UI.
- frontend/components/ResearchArena.tsx is the extension point, not a second lab.
  Existing unrelated working-tree edits must be preserved when implementing.

These are local code observations, not a full production model-quality audit.

## 11. Delivery slices and acceptance

1. Identity/cohort + label audit: exact joins, availability timestamps, cost/mode
   attribution; malformed/duplicate/legacy rows explicitly excluded and counted.
2. Queue projector + read API + Desk table: replay/restart/rotation tests; one
   decision with two fills stays one decision; blocked rows never become PnL.
3. ML dataset/label views: zero trades produces honest zero operational labels;
   separate research population can continue without manufacturing executions.
4. Training/calibration/walk-forward jobs: event-time purge fixtures, train-only
   transforms, untouched test/calibration separation, source/model reproducibility.
5. ML Lab UI: compatible-cohort tables, uncertainty and freshness; public POSTs
   rejected; research cancellation isolated from orders; null never becomes zero.
6. Report-only forward predictions: original prediction frozen before outcome;
   drift/coverage monitored; inference outage has no new effect on current scanners.
7. Promotion review only after evidence passes the existing ladder; no automatic
   roster change, capital unlock or live adapter access.

Run the full Python and frontend suites plus production build for implementation
releases. Prove bounded API latency and scanner latency under a training workload.
Deploy read projections first, then workers in report-only mode. Roll back those
consumers independently without restarting the canonical recorders.

Success: an operator can trace every displayed row and metric to a compatible
data/decision/cost/outcome artifact. More green rows is not the acceptance test.
