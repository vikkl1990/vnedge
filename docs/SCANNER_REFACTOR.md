# Scanner refactor: boundaries and migration

## Scope of this change

This is a behavior-preserving first slice, not a new strategy, live enablement,
or a claim that the bot has an edge. Both registered BTC/ETH V2 continuation
wrappers now inherit a thinner adapter using `strategy/continuation_rules.py`.

The pure rules layer owns:

- Long/short setup masks from already-prepared features.
- Ordered, complete diagnostic failures as an immutable assessment.
- Only three existing threshold inputs via a structural protocol.

It owns no feed, mutable structure engine, context cache, registry, clock,
order manager, account state, or UI. It does not import those components.
The adapter still binds and interprets context, obtains structure health,
annotates prepared frames, and serializes the existing diagnostic schema.

Setup masks share one side-independent predicate and symmetric side rules.
No second 4h structure veto or AVWAP proxy is introduced. IDs, defaults,
registration permissions, ARM identity, next-open timing and exits stay intact.

## Current responsibilities to preserve

| Boundary | Current owner | Must not own |
| --- | --- | --- |
| Durable evidence | Recorder, canonical lake, bar identity | Strategy approval |
| Causal context | MarketRegime and structure engines | Orders or position size |
| Prepared setup predicates | continuation_rules | Data fetching or execution |
| Scanner adapter | HTF continuation V2 and pair wrappers | Capital permission |
| Registration | strategy_registry and scanner_contracts | Dynamic promotion |
| Entry confirmation | Registered runtime clock | Rewriting setup identity |
| Execution | CostGate, sizing, gateway, Order Manager | UI-driven orders |
| Display | Dashboard/Analyst | Firing or relaxing gates |

`FIRE_PATH.md` remains authoritative for ARM/order identity and authority.
Historical implementations remain importable for archived replay. Removing
their files or merging their IDs would not be safe cleanup.

## Compatibility traps identified

V2 diagnostics are explanatory, not the gateway. Its `eligible` field means
side setup present and context available, not “all diagnostic failures empty.”
Preparation uses strict `eq(1)` flags while diagnostics historically accept
truthy numeric values. In particular, a string `"nan"` is truthy after the old
float conversion; numeric NaN is false. Prepared runtime flags are floats.
The extraction preserves this behavior with a characterization test rather
than quietly changing the schema. A follow-up should explicitly version and
normalize external diagnostic inputs, with consumer tests.

Missing required preparation columns still raise; they are not silently filled.
NaN measurements cannot satisfy volume/body/projected-net comparisons.

## Second slice: stateful HTF context

`continuation_context.py` now owns the bound 4h/daily frames, health flags,
weekly trade-VWAP artifacts and live regime machine for V1 and V2. The scanner
keeps compatibility views for existing evidence/runtime readers and delegates
binding, ingestion, as-of identity and regime evaluation to this component.
It still explicitly forwards 4h context to the separate structure engine.

Full preparation installs its replayed regime machine as the live state;
latest-only preparation continues that exact machine. A fresh full replay
replaces it. The regime-column formatter is shared by both paths; permission
flags and latest-only zeroing remain explicit in the scanner.

This is an ownership extraction, not a new health policy or classifier. The
existing rules for health loss, candle retention, artifact validation and
as-of boundaries are preserved. Structure-engine inheritance/lifecycle has not
yet been redesigned.

`tests/test_continuation_context.py` adds isolation, normalization, closed-time
boundary, health recovery, hash/deduplication/retention, artifact validation,
and full-to-incremental machine-handoff tests for V1 and V2. Existing real-data-
shaped preparation parity tests remain in place.

## Remaining migration, in order

1. Continue separating structure preparation from the current V2 → V1 →
   realtime-scanner inheritance chain. HTF context ownership is extracted;
   structure reset, gap and warmup parity fixtures must precede its migration.
2. Extract the remaining scanner families into family-specific rule modules,
   preserving each historical registration and feature schema. Avoid one giant
   universal scoring engine that hides strategy-specific semantics.
3. Generate an explicit scanner inventory joining registration, runtime
   contracts and deployment roster. Distinguish measurement, research,
   observe-only and paper; do not equate “registered” with “running.”
4. Introduce versioned diagnostic serialization and consumer tests. Show data
   readiness, setup readiness, clock outcome and order outcome separately.
5. Replay any behavioral redesign as a separately registered research candidate
   with fixed costs and untouched evaluation. Never relax existing gates merely
   to produce FIRE events.

## Verification

`tests/test_continuation_rules.py` compares masks against the original formula
on 4,096 mixed rows, exhaustively checks all 4,096 diagnostic gate combinations,
checks index/input preservation, and exercises the adapter's short-side contract.
Existing HTF continuation tests cover registry permissions and context/health
behavior. Run these plus the full repository pytest suite before deployment.

This change does not repair coverage holes, create private execution truth,
approve capital, or convert shadow observations to paper orders.
