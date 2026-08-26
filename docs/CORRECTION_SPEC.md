# VNEDGE correction specification

Version 1.1 · 2026-08-25  
Baseline: `aa3f55f`  
Implementation branch: `codex/scanner-runtime-convergence`

This is the governing correction plan. It does not enable live trading, add a
capital strategy, or weaken any gateway, kill-switch, WAL, reconciliation, or
research-promotion invariant.

## Settled architecture

VNEDGE v1 remains one asyncio process with single-writer portfolio and risk
state. The earlier five-process/Redis proposal is withdrawn as an immediate
plan. Its useful goals are expressed as in-process seams:

- typed market and scanner events;
- one canonical scanner engine driven by live or replay events;
- in-memory canonical candle publication with parquet as the durable sink;
- one risk/WAL/order path whose durable journal acknowledgement is awaited;
- bounded working frames and incremental multi-timeframe state.

A later process split requires measured isolation need plus fault-injection
evidence for IPC loss, duplicates, restart ordering, and stale intents. Every
failure must remain fail-closed.

Sub-100ms infrastructure is also evidence-gated. It is not scheduled until a
tick-level hypothesis demonstrates positive gross edge in clean replay.

## Non-negotiable invariants

1. Missing or ambiguous data blocks decisions; it never manufactures state.
2. Every order traverses `PreTradeRiskGateway` and `KillSwitch`.
3. An order intent is durably journaled before submission.
4. Trade-derived canonical candles outrank venue OHLCV.
5. Research cannot mutate the runtime roster or capital allowlist.
6. `CAPITAL_APPROVED` remains empty throughout this correction program.

## Phase 0 status

Phase 0 is complete and audited in
`docs/CORRECTION_PHASE0_AUDIT_20260825.md`:

- timeframe-correct pending expiry;
- safety housekeeping under continuous quotes;
- receipt latency separated from canonical-lake wait;
- one order-book subscription fan-out;
- explicit REST fallback and unknown-exchange rejection;
- canonical tape cleaning in raw-lake research readers;
- one scanner timeframe contract source;
- twelve-bar minimum 4h prerequisite derived from pivot parameters.

The historical 62% L2 touch-rate is not asserted without the original raw
dataset. The correction guarantees the cleaning contract and reports drops.

## Revised delivery order

### 1. Unified scanner engine (Phase 4)

Create one stateful engine contract with `on_closed_bar` and `on_quote`.
Runtime and replay are drivers over that engine. Recorded quote replay must use
clean BBO data and reproduce live intent keys. Feature calculations, arm/fire
lifecycle, exits, costs, and single-book conflicts must not have parallel
implementations. Quote overflow, rejected quote contracts, and re-arms are
first-class evidence.

Done when a recorded live day reproduces the live fires for every rostered
quote scanner and legacy replay/session loops only delegate.

Current implementation slice:

- the runtime scanner protocol exposes `restore`, `on_closed_bar`, and
  `on_quote`;
- live and recorded quote replay construct the same quote-acceptance engine;
- replay applies the canonical BBO cleaner, deterministic candle/quote event
  ordering, and refuses to run quotes beyond the last causal forming bar;
- `scanner_evidence` compares replay and live intent keys plus approval,
  side, entry, stop, quote sequence, and episode inside the audited
  evidence window;
- quote contract rejects, buffer drops, probe resets, and re-arms are exposed
  in runtime statistics and the scanner workspace;
- the evidence CLI accepts canonical lake candles directly (`open_time` and
  Decimal columns are normalized), loads sharded BBO directories, and audits
  an explicit evidence window that excludes warm-up history from parity
  (default window start: first clean recorded quote).

This is mechanism parity infrastructure, not completion evidence. Phase 4 is
complete only after a recorded live day produces an exact parity artifact.

Parity capture runbook (run where the recorded data and journals live):

```
docker compose run --rm scanner-evidence \
  python -m vnedge.research.scanner_evidence \
    --strategy range_expansion_realtime_v1 \
    --symbol "BTC/USDT:USDT" \
    --candles /app/data/candles/exchange=binanceusdm/BTCUSDT/15m \
    --quotes "/app/data/ticks/exchange=binanceusdm/symbol=BTCUSDT/stream=book/<YYYYMMDD>" \
    --journal "/app/logs/paper_trials/<lane_id>.journal.jsonl" \
    --runtime-start <runner-started-at-ISO> \
    --evidence-start <YYYY-MM-DD>T00:00:00Z \
    --evidence-end <YYYY-MM-DD+1>T00:00:00Z \
    --max-bytes-per-journal 134217728 \
    --max-total-bytes 134217728 \
    --out /app/research/live_research/scanner_parity_<YYYYMMDD>.json
```

One invocation per (strategy, symbol) pair; the artifact's `live_parity`
entries must report `exact_parity: true` inside the evidence window. Pin the
capture to the commit the journals were produced by before changing any
runtime event behavior. The journal byte budget must cover the entire pinned
window; the default 8 MiB tail is intended for the rolling dashboard aggregate
and can silently exclude early-window live intents from a full-day comparison.
The audited window must belong to one uninterrupted runner instance; use the
lane heartbeat's durable `started_at` as `--runtime-start` so replay seeds the
same bounded feature history and process-local episode clock.

### 2. In-process event router (Phase 2 prime)

Publish normalized trades, BBO, L2, funding, and canonical closed candles to a
typed in-memory router. The candle event is published before parquet is
upserted asynchronously. Bar lanes consume the event directly; steady-state
decision code performs no parquet polling. Venue candles remain a heartbeat
cross-check and REST candles remain research bootstrap with explicit
provenance.

Done when candle-close event latency p99 is below 250ms, decision paths make
zero steady-state lake reads, and recorder loss blocks lanes within one grace
window.

### 3. Incremental multi-timeframe state (Phase 3)

Use incremental swing updates and canonical 1h/4h events, cap each working
frame, accrue funding into live/paper outcomes, and preserve the full open
round trip across restart. Long-hold contracts express days explicitly and
require a non-zero funding hypothesis.

Done when incremental and full MTF results are bit-exact on a 90-day sample
and per-bar decision cost stays flat with uptime.

### 4. Order-path telemetry (Phase 6, in process)

Measure `created`, `gated`, `submitted`, `acked`, and `filled` without adding
execution IPC. Reject stale intents by contract TTL and journal the rejection.
The gateway remains the only order path.

### 5. ML gates (Phase 5)

Log exact online feature vectors, run meta-label and execution-cost models in
shadow, and permit models only to veto or shrink rule-based signals. Models
never originate direction. Drift may auto-demote; re-promotion is manual.

## Rollout

Every replacement ships dark, runs beside the existing path for at least seven
days, and is compared by journal before deletion of the old path. Parquet
sinks remain stable so rollback does not endanger recorded data.

The overall program is complete only when a recorded live week replays to the
same journal fires, scanner decision cost is flat over prolonged uptime,
decision paths do not poll parquet, and order-hop SLOs are visible. Those are
engineering completion criteria, not evidence of trading edge.
