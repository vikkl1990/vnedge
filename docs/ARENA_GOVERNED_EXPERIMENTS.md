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

The input target must also exist in the configured research universe. The last
20,000 rows are the bounded working input. No sorting, deduplication, gap filling
or identity stamping is performed here. That can expose existing store problems;
it is not permission to fabricate canonical tags on historical OHLC.

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

Up to eight candidates are evaluated per cycle, round-robin across the inventory;
the rest are visibly deferred. Source or contract changes invalidate daily cache.
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

Targeted tests: `tests/test_experiment_packet.py`,
`tests/test_continuous_ai_pipeline.py`, and the Arena component tests.
Tests include bad identities/coverage/gaps, unsupported clocks/context, missing
contracts/data, captured-source binding, write-before-replay, retained failures,
immutable artifact conflicts, cache invalidation, and non-promoting audit results.
