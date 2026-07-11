# Dashboard auth — per-user tokens, roles, expiry

The read-only dashboard (docs/DESIGN.md §6) authenticates every data route
(`/state`, `/history`, `/research`, `/alpha-council`, `/alpha-workbench`) and
the snapshot WebSocket (`/ws`). The static shell page (`/`) stays public — it
contains no data.

Auth is implemented in `src/vnedge/dashboard/auth.py` and wired in
`src/vnedge/dashboard/app.py`.

## Token sources

### `DASHBOARD_USERS` — per-user tokens

```
DASHBOARD_USERS="name:token:role[:expiry_iso];name:token:role[:expiry_iso];..."
```

One entry per user, joined by `;`. Fields per entry, separated by `:`:

| field       | required | values                                                        |
|-------------|----------|---------------------------------------------------------------|
| `name`      | yes      | identity used in logs and the `X-Dashboard-User` header       |
| `token`     | yes      | bearer secret (generate: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`) |
| `role`      | yes      | `viewer` or `operator` (case-insensitive)                     |
| `expiry_iso`| no       | ISO-8601 datetime, e.g. `2026-08-01T00:00:00+00:00`; omit for no expiry. Naive datetimes are treated as UTC. The expiry field may contain `:` — everything after the third colon is parsed as the expiry. |

Example:

```
DASHBOARD_USERS="vik:3KJ...a9:operator;auditor:9fX...q2:viewer:2026-08-01T00:00:00+00:00"
```

Parsing is defensive: a malformed entry (missing fields, empty name/token,
unknown role, unparseable expiry, duplicate name) is **skipped with a loud
WARNING** naming the entry position and problem — the token text is never
logged — and the remaining valid entries still load.

### `DASHBOARD_TOKEN` — back-compat shared token

The original single shared token **keeps working unchanged**: it is loaded as
the user `operator` with role `operator` and **no expiry**. Existing deploys
(docker-compose requires `DASHBOARD_TOKEN` in `.env`) need zero changes. Both
variables may be set at once; all tokens are valid simultaneously — that is
what makes zero-downtime rotation possible.

If neither variable yields at least one user, the dashboard refuses to start
("no token, no dashboard").

## Roles

`viewer` and `operator`. **Both are read-only today** — the dashboard exposes
zero control routes, structurally. The role exists so a future privileged
surface (e.g. the v2 kill-switch button in DESIGN.md §6) can distinguish
operators from viewers without another auth migration. Grant `viewer` by
default; grant `operator` only to people who would be allowed to use such a
control surface later.

## Behavior

- Tokens are accepted via `Authorization: Bearer <token>` header or the
  `?token=` query parameter (the WebSocket uses the query parameter).
- Every stored token is compared with a constant-time comparison, and every
  token is checked on every attempt (no early exit), so timing does not
  reveal which entry matched.
- **Expired tokens are rejected with 401** and an explicit reason
  (`token expired at <iso>`), distinct from `missing or invalid token`.
  A WebSocket whose token expires mid-session is closed (code 4401).
- Authenticated HTTP responses carry `X-Dashboard-User: <name>`.
- WebSocket snapshots include `dashboard_connections`: the **count** of live
  dashboard sockets. Names and tokens are never serialized into snapshots.
- Auth events are logged with name and role only — token values never appear
  in logs, responses, or snapshots.

## Rotation procedure

Zero-downtime, because old and new tokens can coexist:

1. Generate a new token:
   `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.
2. Add it as a **new entry** for the same person in `DASHBOARD_USERS`
   (entries need unique names — use e.g. `vik-2026q3`), optionally giving the
   **old** entry a near-future expiry instead of deleting it immediately.
3. Restart the service (env is read at startup):
   `docker compose up -d multi-lane-shadow` on the VPS, or restart the local
   session.
4. Update the person's bookmark/tunnel URL to the new token; confirm access
   (the `X-Dashboard-User` header shows which entry matched).
5. Remove the old entry (or let its expiry lapse) and restart again.

To revoke a user immediately: delete their entry (or set an expiry in the
past) and restart.

Rotating the legacy `DASHBOARD_TOKEN` is the same dance: set the new value in
`DASHBOARD_USERS` first, restart, verify, then change/remove
`DASHBOARD_TOKEN`.

## Operational notes

- Env changes require a restart — consistent with the frozen-risk-config
  rule; there is no runtime mutation surface for auth.
- Tokens are secrets: keep them in `.env` (gitignored) like every other
  secret in this repo; never commit them.
- The dashboard remains read-only regardless of role; this change adds
  identity and lifecycle to tokens, not capabilities.
