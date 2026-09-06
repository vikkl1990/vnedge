# VNEDGE design spec — adopted requirements for upcoming milestones

Consolidated from the 2026-07-02 architecture reviews. These are the
implementation contracts for milestones 2–6. Anything infrastructure-flavored
from those reviews (UDS daemons, NATS, per-exchange processes) was rejected
for v1 — see CLAUDE.md "Architecture decisions".

## 1. Market data quality gate (milestone 2)

Sits between every exchange feed and everything else. Data that fails a check
never reaches stores, features, or strategies; failures are counted and
alerted, and sustained failure marks the feed unhealthy (which the risk
gateway already rejects on via `exchange_healthy` / `data_freshness`).

Checks, in order:

| Check | Rejects |
|---|---|
| Sequence-gap detection | L2 deltas with missing sequence numbers → full book resync |
| Checksum validation | Corrupt books (Bybit/OKX provide checksums; Binance uses update IDs) |
| Staleness guard | Events older than `max_data_staleness_seconds` |
| Clock-skew monitor | Local vs exchange timestamp drift beyond threshold |
| Spread/depth sanity | Crossed books, empty sides, absurd spreads |
| Mark/index divergence | Mark price far from index → liquidation distortion risk |
| Private-stream freshness | Stale position/order/balance stream → state not trusted |
| Reconnect resync | After any disconnect: full snapshot rebuild before deltas resume |

## 2. Order state machine (milestone 5)

```
SIGNAL_CREATED → RISK_REQUESTED → { RISK_REJECTED | RISK_APPROVED }
RISK_APPROVED → ORDER_INTENT_CREATED → SUBMITTING → ACKNOWLEDGED
ACKNOWLEDGED → { PARTIALLY_FILLED → FILLED | FILLED | CANCEL_REQUESTED → CANCELLED | REJECTED }
SUBMITTING → TIMEOUT_UNKNOWN → RECONCILING → (resolved state)
```

Rules:

- **Idempotency key is minted once at ORDER_INTENT_CREATED**, written to the
  decision journal before submission, and reused verbatim on every retry.
  Never derived from timestamps.
- **TIMEOUT_UNKNOWN is the hardest live failure**: submission sent, no ack.
  While ANY order is in TIMEOUT_UNKNOWN or RECONCILING, the account blocks all
  new risk-increasing orders; reduce-only remains available. Resolution only
  via exchange reconciliation, never by assumption.
- Duplicate-intent registry: same intent key seen twice → second is dropped
  and logged loudly.
- Every submission classifies exchange errors: retryable (rate limit,
  timeout) vs terminal (insufficient margin, invalid symbol) vs
  unknown-state-inducing. Retries use bounded backoff and respect venue rate
  limits.
- Emergency flatten: cancel all working orders, close all positions
  reduce-only, in one idempotent operation. Validated with the bounded
  production mainnet execution drill before any live enablement; testnet data
  and fills are not accepted as scalper execution evidence.

## 3. Exchange reconciliation engine (milestone 6)

Inputs: private WS stream + periodic REST snapshots + internal state.
Compares: positions, open orders, balances, fills, fees, funding payments,
**margin mode, and leverage setting** (drift in the last two silently changes
liquidation math).

Current implementation note: `vnedge.execution.private_stream` consumes
CCXT-Pro private order/fill events, normalizes venue statuses/trades, dedupes
fills by trade id, and applies them through `OrderManager` so every update is
state-machine checked and journaled. This is the real-time order/fill input;
positions, balances, margin mode, and leverage drift still require the
periodic REST reconciliation path before live activation.

Fail-closed rule on any mismatch:
1. Stop opening new positions (risk gateway flag).
2. Enter reduce-only mode.
3. Rebuild internal state from exchange truth.
4. Alert operator.
5. Resume entries only after a clean reconciliation pass.

## 4. Decision journal / WAL (milestone 5)

Local append-only JSONL, written **before** any order submission: risk decision
(all failed/passed checks), intent + idempotency key, explicit submission
boundary, ack, and errors. Schema-2 records carry a monotonic `seq` plus a
SHA-256 `prev_hash`/`hash` chain. Startup validates the tearable tail and
resumes from its last durable sequence; the offline verifier walks the whole
chain. This is a sourced **execution stream**, not an event store for candles
or scanner frames. Market replay remains the canonical trade/candle lake.

Journal-unavailable rule: if the journal cannot be written (disk full,
permission), no new risk-increasing orders; reduce-only exits still allowed.

### Execution identity and evidence boundary

Decision identity and venue idempotency are deliberately separate:

- `decision_id` is minted once at **ARM** as a deterministic digest of
  strategy id/version, symbol, decision timeframe, normalized decision-bar
  content hash, side, frozen permission snapshot id, and entry clock. Quote
  sequence, venue id, and `path_id` are deliberately excluded. Replay uses it
  to compare the same market decision and suppress duplicate decisions.
- `client_order_id` is random, minted exactly once after the risk gateway
  approves and persisted before submission. An ambiguous transport retry or
  restart reuses that journaled value verbatim; it is never derived from a
  timestamp or bar. A definitive venue rejection ends that attempt. A later
  permitted reduce-only resubmission uses a new random id, linked by
  `retry_of`, while retaining the same deterministic decision id.
- `snapshot_id` identifies the immutable permission context at arm time.
- `path_id=kernel_v1` is provenance, not an idempotency key. Only a caller
  that supplies kernel evidence may stamp it.

`path_id` is intentionally not overloaded with market transport or fill-clock
semantics. `ExecutionEvidence` records `candle_source` and `entry_clock`, and
derives `execution_contract_id = path_id|candle_source|entry_clock` for
reporting. `quote_hold`, `next_<tf>_open`, and closed-bar cohorts must never
share an operator P&L headline.

`OrderIntent` is adapter-neutral and contains only the venue instruction.
Strategy, timeframe, bar, quote, HTF, and cost provenance live in the frozen
`ExecutionEvidence` envelope persisted beside the intent and `RiskDecision`.
The adapter receives the intent plus the minted `client_order_id`, never the
evidence envelope.

The stream is append-only by stage: `ARM` creates `DecisionEnvelope`;
`ACCEPT` adds BBO sequence/time/age; `APPROVE` adds CostGate and RiskDecision;
`SUBMIT` adds the venue intent and randomly minted client id; `FILL` adds the
venue fill; `RESOLVE` adds exit, fees, and funding. Every stage retains the
same `decision_id`. New risk without the ARM envelope is refused. Operational
Desk/Journal views project these rows and never synthesize an identifier.

Strategies in `PERMISSION_SNAPSHOT_REQUIRED` must attach the complete
`FrozenPermissionSnapshot`, not only its digest. Its decision and context refs
come from the actual rows selected by canonical context binding, must be closed
no later than the decision bar, and bind trusted source plus candle-content
hashes. Calendar flooring is not sufficient evidence and a missing bound row
refuses the arm.

The one authority boundary is `build_kernel(...)`: OBSERVE evaluates and
journals a candidate without creating a `ManagedOrder`; SHADOW submits through
the simulated adapter; LIVE submits through the live adapter. Research shadow
outcomes without `path_id=kernel_v1` remain evidence-only and are excluded from
execution-readiness and execution P&L. `order_intent` alone is not proof of a
side effect: operational P&L additionally requires an `order_submitted`
descendant for the same `client_order_id`.
The same evidence envelope is repeated on candidate, risk, intent, submission,
fill/reconciliation, and terminal journal events so a projection never has to
join against mutable "latest HTF" state.

## 5. Risk config overrides (v2)

Current `RiskConfig` is global. v2 adds layered overrides resolved as
per-symbol > per-exchange > per-mode > global, all validated by the same
pydantic model so no override can exceed global hard caps.

## 6. Monitoring dashboard (milestone 7)

Measurement-first and out of the execution path. No NATS or bridge daemons — a
small FastAPI app. Market/risk/research surfaces are read-only; the separately
scoped Settings API may mutate encrypted operator configuration but has no
order, promotion, kill-clear, or live-enable authority.

- **Data model: coalesced state snapshots, not event streams.** One snapshot
  object (mode, equity, daily PnL, drawdown, open positions, working orders,
  feed health, kill-switch state, last reconciliation result) pushed over a
  WebSocket at ~1Hz and served at GET /state. Snapshots are complete, so
  reconnects need no replay and bursts can never firehose the browser.
- **Security:** binds to 127.0.0.1 only (VPS access via SSH tunnel); bearer or
  HttpOnly session authentication even on localhost. Settings writes require
  operator scope plus CSRF and are audit-logged. There is no order, promotion,
  kill-clear, or live-enable route.
- **Isolation:** the snapshot is built by the bot's medium loop and handed to
  the UI server as an immutable object; a slow or dead browser can never
  block trading. Failed WebSocket sends deregister the client immediately.
- Frontend: single static HTML page, vanilla JS, auto-reconnect with backoff.
  No build step. Telegram alerts (already planned) remain the primary
  operator channel; the dashboard is for inspection, not operation.
- Config (env): `DASHBOARD_HOST=127.0.0.1`, `DASHBOARD_PORT=8080`,
  `DASHBOARD_TOKEN` (back-compat shared token) and/or `DASHBOARD_USERS`
  (per-user tokens with roles + expiry — see docs/DASHBOARD_AUTH.md; at
  least one user required — no token, no dashboard),
  `DASHBOARD_SNAPSHOT_HZ=1`.
- Snapshot DTO fields: ts, mode, live_trading_enabled, kill_switch_active,
  equity, realized_pnl, unrealized_pnl, daily_loss, consecutive_losses,
  risk_status, feed_health {exchange, candles, funding, open_interest,
  last_update_ms}, positions[], open_orders[], last_risk_reject,
  last_journal_write. Every message self-contained.
- Hard invariants: dashboard failure never affects strategy/risk/orders/
  journal/reconciliation; cannot place orders, change risk config, unlock
  live trading, or disable the kill switch; degrades by dropping UI updates,
  never by slowing the bot; safe to close at any time.

## 7. Explicitly deferred

- Options / Greeks engine, IV surface, expiry risk: **v3**, separate risk
  model, never mixed into the perps path.
- Model registry + canary promotion: v2 (no auto hot-swap ever; human
  approval gate is permanent).
- TimescaleDB historian: v2 (Parquet + SQLite sufficient at v1 volume).
