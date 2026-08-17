# Dashboard auth — per-user tokens, roles, expiry

The measurement dashboard authenticates every data route
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
| `token`     | yes      | bearer secret — plaintext, **or** a salted hash (see *Hashed tokens* below) so the env holds no usable secret |
| `role`      | yes      | `viewer`, `operator`, or `auditor` (case-insensitive)         |
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

### Hashed tokens — the env holds no usable secret

The `token` field may be a **salted hash** instead of a plaintext secret, so a
leaked `.env` / compose config does not leak a working token. Generate one from
a raw token read on stdin (never argv, so it stays out of shell history / `ps`):

```
python -m vnedge.dashboard.auth hash
# paste/pipe the raw token when prompted; it prints e.g.
# vnedge-sha256$<salt_hex>$<digest_hex>
```

Put the printed `vnedge-sha256$…` value in the `token` field (in
`DASHBOARD_USERS` or as `DASHBOARD_TOKEN`); keep the **raw** token in your
password manager. The dashboard hashes each presented token with the stored
salt and compares constant-time. Plaintext tokens still work unchanged — a
stored value is only treated as a hash when it starts with `vnedge-sha256$` —
so this is opt-in and backward-compatible. Tokens are high-entropy secrets, so
a salted SHA-256 is sufficient (a slow password KDF would add latency to every
request and buy nothing against a 32-byte random token).

## Roles

`viewer`, `operator`, and `auditor`, mapped to permissions in
`auth.PERMISSIONS`:

| role       | permissions                                                       |
|------------|-------------------------------------------------------------------|
| `viewer`   | `view`                                                            |
| `auditor`  | `view`, `view_audit` (read the operator-action trail; **no** control) |
| `operator` | `view`, `view_audit`, `promote`, `flip_live_gate`, `kill_switch`  |

Market, runtime, risk, journal, research, and promotion-evidence routes are
read-only. The Settings API is the sole mutation surface: an operator may store
scoped profile/connection configuration and encrypted credentials. It cannot
place orders, change the capital roster, promote a strategy, clear a kill
switch, or enable live trading. Browser mutations additionally require the
session CSRF cookie/header pair. A viewer receives `403` from these routes.
`GET /whoami` returns the caller's identity and permissions so the frontend can
hide unavailable controls; the server remains authoritative.

## Health & readiness probes

Two **unauthenticated** probes (they reveal only process state, never data):

- `GET /health` — liveness: `200 {"status":"ok"}` as soon as the process
  serves. Used by the compose healthcheck + TLS proxy.
- `GET /ready` — readiness: `200 {"status":"ready"}` once a snapshot has been
  published, `503 {"status":"starting"}` while warming. Lets an orchestrator
  wait for data-readiness without treating a warming process as dead.

## Behavior

- Open `/app/` with no credential in the URL. If no session exists, the UI
  prompts for the root token and submits it once in an
  `Authorization: Bearer <token>` header to `POST /auth/session`.
- The session JWT is returned only in a `Secure`, `HttpOnly`, `SameSite=Strict`
  cookie. It is never present in JSON, browser storage, or WebSocket URLs.
- Browser HTTP requests and both WebSocket streams authenticate with that
  same-origin cookie. WebSockets reject query-string credentials.
- HTTP `?token=` remains a deprecated compatibility path for old API clients.
  Do not use it in browsers: URLs leak into history, proxy logs, referrers, and
  screenshots. New automation must use the bearer header.
- Every stored token is compared with a constant-time comparison, and every
  token is checked on every attempt (no early exit), so timing does not
  reveal which entry matched.
- **Expired tokens are rejected with 401** and an explicit reason
  (`token expired at <iso>`), distinct from `missing or invalid token`.
  A WebSocket whose token expires mid-session is closed (code 4401).
- Authenticated HTTP responses carry `X-Dashboard-User: <name>` and
  `X-Dashboard-Role: <role>`.
- WebSocket snapshots include `dashboard_connections`: the **count** of live
  dashboard sockets. Names and tokens are never serialized into snapshots.
- While the React dashboard remains open, it renews the short-lived HttpOnly
  session every eight minutes through `POST /auth/session/refresh`. Renewal
  requires both the valid session cookie and the CSRF cookie/header pair; an
  expired session still requires a fresh root-token bootstrap.
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
4. Open the unchanged `/app/` bookmark, enter the new token on the sign-in
   screen, and confirm access (the `X-Dashboard-User` header shows which entry
   matched). The token is never part of the bookmark.
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
- Operational market and trading surfaces remain read-only. Only the scoped
  Settings API mutates state, with operator authorization, CSRF validation,
  encryption-at-rest for secrets, and an append-only operator audit record.
