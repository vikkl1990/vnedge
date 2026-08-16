# Agent Gateway

The Agent Gateway is a bearer-authenticated, read/research-only HTTP surface.
It can inspect snapshots and artifacts and enqueue strict backtest tasks. It has
no paper or live order authority.

## Safety contract

- Every submitted backtest requires `strict_mode=true`.
- `live_orders_enabled` must be `false`.
- Arbitrary code execution and strategy-source mutation are not supported.
- Tasks cannot change the capital roster or promotion state.
- Removed scanner and Pine routes are not mounted.
- Responses and durable tasks retain `can_trade=false` and
  `can_promote=false`.

## Main routes

All routes are under `/api/agent/v1` unless otherwise noted.

| Route | Purpose |
| --- | --- |
| `GET /health` | Gateway capabilities and route inventory |
| `GET /whoami` | Authenticated agent identity and scopes |
| `GET /state` | Current read-only runtime snapshot |
| `GET /lanes` | Normalized lane measurements |
| `GET /research/latest` | Latest continuous-research artifact |
| `GET /jobs` | Durable backtest job inventory |
| `POST /backtests` | Enqueue a strict, research-only backtest |
| `POST /v2/tasks` | Enqueue a typed durable research task |
| `GET /v2/events` | Read durable task events |
| `GET /v2/artifacts` | Read durable task artifacts |

The exact route list is also returned by `GET /health`; treat that runtime
response as authoritative.

## Authentication

Configure agent tokens and scopes through the existing gateway token store.
Use the `R` scope for reads and the narrowly scoped job/task permission for
research submissions. Authentication failures are audited and fail closed.

## Starter jobs

`python -m vnedge.agent_gateway.seed_jobs` creates only strict research jobs.
The seed set excludes removed scanners and scalpers. Seeding is idempotent by a
stable request signature and never starts a runtime lane.
