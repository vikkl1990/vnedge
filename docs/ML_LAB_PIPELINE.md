# ML Lab: controlled evidence pipeline

Built 2026-09-13. Research/report-only. **Code completion is not model readiness,
economic evidence, a deployment, or permission to trade.** No roster, CostGate,
scanner, capital or promotion rule changes are part of this build.

## Data flow

Full verified journal + fill-ledger prefixes
→ flat-to-flat paper accounting + settled-funding coverage
→ exact decision/feature join
→ write-once cohort dataset
→ registered chronological training / calibration / test
→ immutable calibrated research model + measured holdout report
→ explicitly started report worker
→ prediction-before-exit feedback on the same decision ID.

The bounded feature audit remains separate. Its counts cannot certify a full
ledger prefix. The status worker now projects both audits and controlled-run
artifacts. Dashboard GETs never train, deserialize models or start workers.

## What is implemented

* `ml/ledger_labels.py`: reads and verifies the same bytes it consumes, requires
  schema-2 journals from genesis and a complete fill chain; rejects symlinks,
  torn tails, conflicting IDs, overlapping owners, reversals, mixed books,
  quantity/fee mismatches and unresolved positions. Supports partial exits,
  one entry owner, long and short positions. Duplicate events never add labels.
* Runtime paper fills now carry `executed_at`, independently of ledger write
  time. Untimed historical simulations do not become operational evidence.
* Paper reconciliation emits an `ml_accounting_checkpoint` binding the durable
  fill prefix to venue order quantities, fees, open positions, unresolved orders
  and journal recovery health. It is telemetry, not a new risk bypass.
* Net labels use realized cash flow minus actual recorded fees and verified
  funding. They do **not** subtract the pretrade gate wall, or a second flat fee.
  Net bps are normalized by actual entry notional, not leverage or margin.
* `ml/lab_pipeline.py`: immutable plans, datasets, source/model hashes, event-time
  purging, one consumed attempt, fixed HGB training, separate sigmoid calibration,
  AUC/Brier/log-loss/reliability bins and after-cost baseline/selected statistics.
  Drawdown is explicitly in cumulative unweighted trade bps, not portfolio DD.
* `research/ml_forward_reports.py`: opt-in, post-ARM report worker. Fresh exact
  feature/envelope joins only, fixed model hash, exclusive prediction creation.
  No call into the execution adapter, strategy registry or promotion machinery.
* ML Lab UI: registered plans, frozen datasets, ledger exclusions, run counts,
  train/calibration/test sizes, purges, holdout metrics and forward reports.

## Mandatory input contracts and remaining real-data blockers

1. **Funding coverage is not inferred.** `funding_applied` proves a cash movement,
   not that all settlements were seen. The finalizer requires an independently
   verified `ml_funding_coverage` receipt in the lane journal:

   ```json
   {
     "symbol": "BTC/USD:USD",
     "start": "2026-09-13T00:00:00+00:00",
     "end": "2026-09-13T08:00:00+00:00",
     "complete": true,
     "source_sha256": "<64 lowercase hex characters of the archived settled-history evidence>",
     "events": [{"event_id": "<same settlement identity as funding_applied>",
                 "ts": "<UTC settlement time>", "rate": 0.0001}]
   }
   ```

   The receipt is recorded after its coverage interval ends. Rates, IDs, cash
   movements and dates must match. Conflicting overlapping receipts quarantine
   the episode. Empty events are valid only with genuinely complete source
   coverage. A missing feed, an indicative funding rate, or a schedule rollover
   is **not** such proof. **No producer is enabled to assert this coverage from
   the current feed. The funding-evidence worker now archives the public Delta
   FUNDING candle responses with immutable source hashes, but these are rate
   history, NOT itemized settlement proof. Its status explicitly says
   `settlement_verified=false`. An authoritative settled-history/payment source
   is still required. Do not append synthetic `complete=true` receipts.**

2. **Features must exist before entry** for this dataset contract. `captured_at`
   is when the input snapshot was taken; `ts` is when its computed vector was
   recorded. Neither timestamp is backdated to bar close. PAPER lanes now
   compute the vector on the strategy preparation worker before signal/entry,
   then persist the small identity-bound vector with fsync before entry submit.
   A missing hash or write failure is fail-soft for execution and fail-closed
   for labels. Non-fires, legacy/quote observations and startup history keep
   the asynchronous path; late records remain rejected. Existing exits run
   before preparation, and no entry timing threshold is relaxed. Capture path
   is explicit (`pre_entry_prepared_v1` vs `async_observation_v1`).

3. **Missing feed values are not observations.** Warmup and unavailable inputs
   are recorded explicitly. No funding/OI/benchmark feed or no aggressor-volume
   coverage means the corresponding selected features are inadmissible, even
   when legacy feature math returns a neutral zero. Feature subsets are frozen
   in the plan, not dropped after inspecting the outcomes. Existing indicator
   formulas/fingerprints are not changed by this work.

4. **This version supports paper ledger outcomes only**, not invented shadow
   fills or live venue accounting. Open or ambiguous episodes remain censored.
   The label target is `reconciled_paper_net_positive_v1`; profitability of these
   paper outcomes is not evidence of executable live edge.

## Controlled workflow

The artifact root is `research/ml_lab` (operator-owned; never publicly writable).
Do not load downloaded pickle/joblib files. Only locally generated artifacts
whose hashes match their registered result are accepted by the report worker.

Register a JSON `LabPlan` with:

* `name`, `purpose` (`exploratory` or `prospective`), ordered `features`;
* exact `cohort`: strategy ID, exchange, symbol, timeframe, mode=`paper`, entry
  clock, cost profile ID **and configuration SHA**, label contract, fingerprint;
* timezone-aware `train_start < calibration_start < test_start < test_end`;
* `embargo_seconds`, floors (at least 200 training, 50 calibration, 50 test rows),
  and `probability_threshold` (default 0.60, frozen before fitting).

```bash
.venv/bin/python -m vnedge.research.ml_lab_run register /absolute/path/plan.json
.venv/bin/python -m vnedge.research.ml_lab_run freeze PLAN_ID --lane-dir logs/paper_trials
.venv/bin/python -m vnedge.research.ml_lab_run run PLAN_ID DATASET_ID
.venv/bin/python -m vnedge.research.ml_lab_run status
```

Registration captures implementation and library versions. Any subsequent change
requires a new reviewed plan. Prospective tests must start after registration;
overlapping prospective windows for the same cohort cannot be reserved twice.
This new pipeline refuses history before 2026-09-13, protecting the charter's
historical judgment windows. It does not retroactively certify untouched data.

Every split uses decision time for membership and requires the outcome's final
**availability time plus embargo** before the next boundary. Training and
calibration must each contain both classes. Insufficient data blocks before an
attempt; once fitting starts, a failure/crash consumes the attempt. There is no
automatic retry, tuning, winning-threshold selection or capital activation.

After a reviewed research model exists, explicitly start reporting:

```bash
.venv/bin/python -m vnedge.research.ml_forward_reports \
  --plan-id PLAN_ID --lane-dir logs/paper_trials --interval-seconds 10
```

Reports must concern decisions after model creation and within 120 seconds of
decision close. A report is **post-ARM**, not an entry gate. Only reports before
the eventual reconciled exit may join feedback. Test predictions are stored in
a separate file and never counted as forward observations.

## Validation boundaries

The included end-to-end fixture has 343 deliberately synthetic episodes. It
tests 220 training / 61 calibration / 62 holdout rows, immutable attempts and
forward binding. It is not a backtest or trading result.

New plans can freeze `development_cpcv=true`: six groups/two test groups,
actual decision-to-label-availability interval purging plus embargo, separate
calibration in each fold, all fold sample floors enforced. Only development
data available before the final holdout is used. Repeated OOS predictions are
aggregated by decision; fifteen folds are NOT fifteen independent datasets.

Register an exact 2–8-plan family BEFORE its runs and holdout start:

```bash
.venv/bin/python -m vnedge.research.ml_lab_run register-family PLAN_ID_1 PLAN_ID_2
.venv/bin/python -m vnedge.research.ml_lab_run validate-family FAMILY_ID
```

All family members must share the exact cohort and holdout. Missing/failed
members block validation, not disappear. Hash-bound test predictions must
cover identical outcomes (no convenient inner join). The family report uses
at least 32 full UTC days, charges the raw registered trial count for DSR and
uses tie-aware CSCV PBO. Returns are daily sums of unweighted net trade bps,
NOT portfolio equity returns. Family deflation does not account for undisclosed
historical searches. It does not establish live execution or funding accuracy.

The separate human promotion ladder remains mandatory.
`can_trade=false`, `can_promote=false` throughout.
No real plan, trained production model, forward worker or protected-data test is
automatically created by this build. Real label count can correctly remain zero.

Deployment, when requested, needs rebuilt lane/status/UI images; the status
service mounts `research/ml_lab` read-only. A trained model or report worker must
not be enabled merely because the new UI exists.

## September 14 deployment scope and remaining external evidence

Deploy lane/status/UI and the public funding-evidence archive worker. Preserve
all roster/capital controls; do not create a paper lane to manufacture labels.
The collector has no credentials and never writes lane journals or books cash.
Delta websocket schedule rollover no longer fabricates settled prints; old
journals remain frozen. This is an accounting correctness patch, not a signal
change. Funding-dependent old observations are not upgraded into verified ML.

Still required: an authoritative Delta settlement source/contract, complete
real reconciled paper episodes under an approved roster, enough labels in each
registered time split, and successful prospective/forward economic validation.
These cannot be completed by a code deployment or synthetic fixtures.

## Streaming audit and action list

The September 14 local completion slice streams large journal and feature
prefixes without retaining evaluation telemetry in memory. It adds explicit
accounting-lane inventory and per-lane failure reasons to ML Lab. Infrastructure
journals are disclosed separately; an empty inventory never certifies coverage.
See [implementation limits and remaining external requirements](PENDING_EVIDENCE_BUILD.md).
