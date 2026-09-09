# Arena governed experiments v1

Research only. This extends the existing continuous pipeline; it does not
introduce a second trading engine, an autonomous promotion path, or ten agents.
The pipeline ID is `continuous_ai_research_v2` so old unnamed-cost results are
not presented as runs of this contract.

## Responsibilities

1. The existing finite blueprint generator proposes at most one source per
   cycle. It now writes a source-bound `.experiment.json` beside the source.
   External candidates must provide the same contract. Existing catalog source
   is never changed to add metadata; identical known source can acquire a
   research contract. This is not retrospective untouched-data preregistration.
2. Deterministic preflight rejects missing identity, source-hash mismatch,
   incomplete or non-consecutive bars, bad coverage, future/forming rows,
   insufficient warmup and unsupported execution/context requirements.
3. The worker saves exact Parquet inputs, captured source and a hashed packet
   **before** running the existing causality analyzer and walk-forward engine.
4. A separate deterministic falsifier inspects only packet and machine results.
   It preserves verified checks, challenges and unknowns. It is explicitly not
   an independent LLM, an AI consensus, or an economic proof.
5. Arena projects those records, including NOT_TESTABLE and unresolved gaps.

## Candidate contract

The Pydantic schema is `research.experiment_packet.ExperimentSpec`, with unknown
fields forbidden. Required fields: strategy ID (including `ai_`), source SHA256,
claim, falsification condition (`invalidation`), and named cost profile. Target,
train/test sizes, max hold, exact-volume requirement, context and entry clock
are frozen with it. The built-in candidates explicitly remain 1h next-open EMA
experiments on Delta BTC with `delta_swing`; they have not become scalpers.

V1 supports canonical Delta BTC/ETH decision data and the existing next-open
engine. Quote-hold/maker claims return `lane_bbo_replay_required`; context-bound
claims return `bound_context_replay_required`. No synthetic or other-venue
fallback occurs on this path. Unknown or missing contracts remain NOT_TESTABLE.
The standalone generic research API is retained for legacy fixtures; the
continuous Arena worker always supplies `experiment_dir` to choose governance.

The input target must also exist in the configured research universe. Production
and the standalone CLI use `CanonicalResearchStore`, not generic `ParquetStore`.
The root is `AI_CANONICAL_CANDLE_ROOT` (default `data/candles`), partitioned by
`exchange=delta_india/BTCUSD|ETHUSD/timeframe`. Compose mounts canonical candles
read-only. Venue-native symbols are mapped to the explicit canonical partition;
contradictory row identities fail. Persisted open/close, source, closed flag,
quality, coverage and content hash are mandatory. Decimal columns undergo the
same float conversion as the recorder's decision hash; the stored hash is never
replaced. Preflight verifies it against the actual feature representation.

The last 20,000 rows are the bounded working input. Files are read in partition
order, without row sorting, deduplication, gap filling or provenance upgrades.
Missing Delta history never falls back to generic/official/other-venue OHLC.
The reader does not manufacture longer history or change research window sizes.

External candidates need an explicitly authored full `ExperimentSpec`. Bind it
with `python -m vnedge.research.experiment_contract SOURCE.py --spec SPEC.json`.
The command validates the sandbox source, strategy ID and exact source hash,
then writes the sidecar once. It cannot infer a claim or overwrite an existing
contract. It is research admission, not strategy registration or promotion.

## Evidence and attempts

Under the existing research output directory:

- `experiments/sources/<hash>.py`: source actually loaded for evaluation.
- `experiments/inputs/<hash>.parquet`: exact frame evaluated, including schema.
- `experiments/packets/<hash>.json`: source/spec, input hash, full backtest config,
  gate values, cost vector, entry/fill assumptions, split/grid, preflight cutoff,
  Python/library versions and current source-tree fingerprint.
- `experiments/attempts/<id>.started.json`: durable record before work.
- `experiments/attempts/<id>.result.json`: result, packet ID and falsifier output.

Started without result means incomplete, not successful or a zero-trade run.
Content-addressed records are exclusive-create and fsynced. Changed/torn existing
bytes cause an error rather than overwrite. The normal cycle evidence/feed still
references the candidates; execution WAL and ManagedOrder remain untouched.

Up to eight candidates are attempted per batch; the rest are visibly deferred.
The pipeline drains deferred work on hourly eligibility while retaining completed
attempts. Daily retest timing stays anchored to the start of that inventory sweep,
not the last batch. Source/contract changes invalidate the cache and start a new
sweep. `canonical_queue_v2` invalidates older generic-input cached evaluations.
Next-queue and next-retest timestamps survive cached cycles; they are eligibility
times, not a promise the outer worker (which also does other research) is idle.
UI counts distinguish discovered, attempted, deferred, causality-evaluated and
backtested candidates. All candidates, including the deferred tail, are visible.
The parameter grid contains one empty parameter set: this wrapper does not tune.
These are workload bounds, not an OS security boundary. Existing worker/container
restrictions and AST validator still apply. No new credential or network tools
are granted, and this change does not claim to harden Python sandbox isolation.

## Economics and limitations

Costs come from the named existing `BacktestConfig.cost_profile`, not a new tariff
table. Taker entry and exit costs are recorded separately from the profile's
safety buffer; there is no claim this candle replay ran operational CostGate.
Funding is explicitly excluded and venue product limits are unverified research
assumptions. The benchmark is flat cash (zero net), not a fitted control strategy.

Every result remains `performance_eligible=false`, `can_trade=false`, and
`can_promote=false`. A positive rolling result still needs cost-stress replay,
verified venue limits, funding, execution parity and separately authorized
untouched-data judgment. No historical window is relabeled untouched. This
adapter does not provide a protected holdout vault; only exploratory data should
be mounted into its store. No live roster, scanner rule, or capital setting changes.

## Verification

### Delta repair boundary

The Delta owner now runs a bounded quality-aware repair loop after recording
starts, then every 900 seconds. It replaces the trust-all raw startup bootstrap.
The loop holds inherited venue write authority and serializes partition updates
with the recorder's file locks. It does not run Binance recovery commands.

`data/reports/delta_lake_repair.json` separates verified history, missing verified
slots, raw day inventory and repaired partial rows. Arena projects this audit.
Raw days do not imply continuous days or a completeness manifest.

- A missing closed flag may be restored ONLY on an already attested canonical
  row whose independent tape replay matches the stored content hash.
- Pre-provenance rows with contract units can be rebuilt only when the full
  observed candle matches raw replay after one frozen Delta multiplier. They
  are marked `source=repaired`, `data_quality=partial`, `coverage_ok=false`.
  This corrects units; it does not manufacture eligible volume or history.
- Exact pre-change partition bytes are backed up under
  `data/repairs/delta/backups/<sha256>.parquet`.
- Missing parents are rebuilt only from all closed, hash-verified, coverage-ok
  children. Existing partial/missing minutes remain rejected. Replay failure,
  absent metadata, or a hash mismatch is not permission to overwrite truth.
- Replay is bounded to two closed days per cycle and two million raw rows per
  day (including adjacent partitions). No historical REST completeness claim.

The existing 2,160-hour Arena window remains unchanged. Repairing a short lake
does not create 90 days. A longer complete official public trade archive or a
separately verified tape manifest is still required to backfill missing history.

### Per-attempt data readiness diagnostics

New preflight packets contain `data_readiness` scoped to their exact supplied
frame and capture time. Arena exposes stored rows versus the contract requirement,
independently verified unique bars, the longest continuous verified run, the
continuous verified tail, row and continuous-run shortfalls, and internal missing
open-time ranges (last 32 displayed; full counts retained). Proof failures are
counted separately from absent rows; they use the first failed proof per row.
Duplicate slots are excluded from verified history, not deduplicated into it.
Reversed input order is explicit even if individual hashes pass.

These are diagnostics, not another admission gate. Existing preflight decides
admission unchanged. A usable sub-run never authorizes clipping the frozen
train/test input; historical coverage outside the supplied frame stays unknown.
No ETA assumes future gap-free collection. No repair command or automatic
backfill is granted by this view, and `can_trade`/`can_promote` remain false.

Packets and old completed attempts are not rewritten or rerun to populate the
new fields. Legacy cached attempts show diagnostics unavailable until the normal
scheduled preflight captures a new report. Data-reader failures remain explicit
NOT_TESTABLE reasons; absent reports must not be displayed as zero gaps.

Scoped rollout uses `VNEDGE_DEPLOY_SERVICES="delta-recorder multi-lane-shadow research-loop agent-job-runner" bash scripts/deploy.sh`.
It retains the deployment lock and build-before-recreate rule, uses a fast-forward
merge, and verifies serving SHAs without claiming runtime readiness.

Targeted tests: `tests/test_arena_canonical_input.py`, `tests/test_experiment_packet.py`,
`tests/test_continuous_ai_pipeline.py`, and the Arena component tests.
Tests include bad identities/coverage/gaps, unsupported clocks/context, missing
contracts/data, captured-source binding, write-before-replay, retained failures,
immutable artifact conflicts, cache invalidation, and non-promoting audit results.
## Verified recovery planning

The canonical owner's existing maintenance loop publishes a separate,
read-only `data/reports/delta_recovery_plan.json` after its repair audit.
The research worker projects that report into Arena; it does not rerun completed
experiments or modify their frozen contracts when the report changes.

The inventory covers the last 2,160 **closed** UTC hours per BTC/ETH symbol,
including leading missing history, not only internal holes. It validates
persisted closed/coverage booleans, source, content hash, partition identity,
alignment and candle geometry before using a child. Duplicate slots are invalid.
Missing parents can be described through the 1m → 5m → 15m → 1h chain; existing
invalid rows are never replacement candidates. A target partition with missing
legacy metadata blocks a proposal because upserting there could upgrade other
rows accidentally.

Hourly classifications are `VERIFIED`, `REBUILD_FROM_VERIFIED_CHILDREN`,
`PRESENT_PROOF_INVALID`, `TARGET_PARTITION_UNSAFE`,
`RAW_DAY_PRESENT_COVERAGE_UNPROVEN`, `NO_LOCAL_RAW_DAY`, and `INPUT_UNREADABLE`.
Raw-day filenames are inventory hints only. There is no supported historical
raw-completeness manifest in this slice, so raw replay is **not authorized**.
Official OHLC must not fill this canonical research window.

Each report carries exact input-partition byte hashes and a plan hash. Reads
are not an atomic cross-partition snapshot; plans expire as inputs change and
the canonical owner must revalidate before any repair. The planner cannot apply
anything. It cannot trim an experiment window, relax preflight, promote, or
trade. A verified rolling inventory is not admission for a different frozen
experiment window.

Budgets: 250,000 scanned rows per timeframe, 128 MiB per partition, at most 256
displayed missing ranges and 128 detailed parent candidates, with full counts
and truncation flags. Truncated candidates are not an executable repair queue.
Planning errors are recorded separately and do not erase the repair audit.

Read-only audit (JSON to stdout; no report/candle writes):

```sh
.venv/bin/python -m vnedge.data.delta_recovery_plan \
  --data-root data --candle-root data/candles --symbol BTCUSD
```

### Read-only VM audit, 2026-09-09 16:24–16:25 UTC

The planner was executed from stdin in the research container against its
read-only candle mount, without installing a writer or saving candle changes.
Window: 2026-06-11 16:00 UTC through 2026-09-09 16:00 UTC, end exclusive.

| Hourly classification | BTC | ETH |
| --- | ---: | ---: |
| Verified | 201 | 200 |
| Present, proof invalid | 7 | 7 |
| Raw day present, interval coverage unproven | 600 | 601 |
| No local raw day | 1,352 | 1,352 |
| Reconstructable from verified children | 0 | 0 |

These are observations, not a backtest result or a repair success. Both frozen
2,160-bar experiments remain ineligible. The preceding coverage diagnostics
release `9536de6d8d94e2149aadf0381479a01d98f13faa` is deployed; this recovery
planner is a subsequent build and must be deployed separately before the
owner publishes its report automatically.
