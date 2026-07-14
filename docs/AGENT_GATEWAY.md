# VNEDGE Agent Gateway

The Agent Gateway is the first slice of the **VNEDGE AI Quant OS** surface:
an AI-facing, scoped API under `/api/agent/v1` that lets research agents read
the bot's state and request research work without touching exchange keys,
strategy source, order routing, or live controls.

It is deliberately separate from the human dashboard API:

- dashboard routes use `DASHBOARD_TOKEN` / `DASHBOARD_USERS`;
- agent routes use `AGENT_GATEWAY_TOKENS_JSON`;
- dashboard tokens do not authenticate agents;
- agent tokens do not authenticate the dashboard.

## Safety Boundaries

This first phase is research-only.

- No order route is exposed.
- No live trading route is exposed.
- Backtest requests are recorded as `PENDING_RESEARCH_ONLY` jobs.
- Every job has `can_trade=false`, `can_promote=false`, and
  `live_orders_enabled=false`.
- Every authenticated agent route appends a hash-chained JSONL audit record.
- Tokens are paper-only by default.

The existing VNEDGE ladder remains the only route toward capital:

```text
research -> untouched judgment -> human approval -> paper -> shadow -> live_small -> live_full
```

## Configure Tokens

Set `AGENT_GATEWAY_TOKENS_JSON` on the dashboard/multi-lane service. Empty
means the gateway is not mounted.

Prefer `token_sha256` in production:

```bash
python3 - <<'PY'
import hashlib, secrets
token = "agd_" + secrets.token_urlsafe(32)
print("token:", token)
print("sha256:", hashlib.sha256(token.encode()).hexdigest())
PY
```

Example `.env` value:

```json
[
  {
    "name": "alpha-council",
    "token_sha256": "REPLACE_WITH_64_HEX_SHA256",
    "scopes": ["R", "B"],
    "paper_only": true,
    "rate_limit_per_min": 60,
    "markets": ["binanceusdm:BTC/USDT:USDT", "bybit:BTC/USDT:USDT"],
    "lanes": ["*"]
  }
]
```

For local development only, `"token": "raw-secret"` is accepted and hashed
immediately at startup. The raw token is not retained on the token object.

## Scopes

| Scope | Meaning |
|---|---|
| `R` | Read gateway-safe bot state and research artifacts. |
| `B` | Submit research-only backtest job requests. |
| `W_RESEARCH` | Reserved for future sandboxed research artifact writes. |
| `T_PAPER` | Reserved for future paper-only agent actions. No route uses it yet. |

There is intentionally no live-trading scope in this phase.

## Routes

All routes except `/health` require `Authorization: Bearer <agent-token>`.

| Route | Scope | Purpose |
|---|---:|---|
| `GET /api/agent/v1/health` | none | Gateway status and available route list. |
| `GET /api/agent/v1/whoami` | token | Token identity, scopes, allowlists, paper-only flag. |
| `GET /api/agent/v1/state` | `R` | Latest coalesced dashboard snapshot. |
| `GET /api/agent/v1/lanes` | `R` | Agent-friendly lane summary distilled from the snapshot. |
| `GET /api/agent/v1/research/latest` | `R` | Latest continuous-research verdict payload. |
| `GET /api/agent/v1/alpha-council` | `R` | Latest deterministic council debate output. |
| `GET /api/agent/v1/alpha-workbench` | `R` | Latest proof-task backlog. |
| `GET /api/agent/v1/vibe-intelligence` | `R` | Latest hypothesis lifecycle memory. |
| `GET /api/agent/v1/lane-readiness` | `R` | Latest paper/shadow promotion readiness report. |
| `GET /api/agent/v1/realtime-scanner` | `R` | Latest live-observation scanner report. |
| `GET /api/agent/v1/jobs` | `R` | List agent research jobs. |
| `GET /api/agent/v1/jobs/{job_id}` | `R` | Read one agent research job. |
| `POST /api/agent/v1/backtests` | `B` | Record a strict, research-only backtest request. |

## Backtest Request Example

```bash
curl -sS \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy_id": "sats_5m_scalper_v1",
    "exchange": "delta_india",
    "symbol": "ETH/USDT:USDT",
    "timeframe": "5m",
    "hypothesis_id": "sats-eth-delta-bbp-stealthtrail",
    "strict_mode": true,
    "live_orders_enabled": false,
    "parameters": {"note": "agent-requested exploratory replay"}
  }' \
  http://127.0.0.1:8080/api/agent/v1/backtests
```

The response is a job ledger record, not a promotion:

```json
{
  "job_id": "agj_...",
  "kind": "backtest_request",
  "status": "PENDING_RESEARCH_ONLY",
  "can_trade": false,
  "can_promote": false,
  "live_orders_enabled": false
}
```

## Audit Files

Defaults:

- `AGENT_GATEWAY_AUDIT_PATH=logs/agent_gateway/audit.jsonl`
- `AGENT_GATEWAY_JOBS_DIR=logs/agent_gateway/jobs`

Each audit record includes `prev_hash` and `hash`, so the gateway call stream
is tamper-evident without mixing it into the order decision journal.

## Next Phases

1. Agent Gateway OpenAPI document and MCP wrapper.
2. A worker that consumes `PENDING_RESEARCH_ONLY` jobs and runs approved
   backtest/replay commands.
3. Sandboxed strategy-package authoring with AST/import validation.
4. UI panels for agent tokens, jobs, and audit trail.
5. Paper-only agent action scope, after a separate review.

