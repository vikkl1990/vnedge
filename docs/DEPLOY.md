# Deploying VNEDGE

VNEDGE now deploys as a measurement-first, no-live-order stack. Docker Compose
contains only the measurement dashboard runtime, an optional research loop, and
the TLS proxy.

## Required configuration

Create `.env` from `.env.example` and set a strong dashboard token:

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Copy the generated value into `DASHBOARD_TOKEN`. Leave the capital controls at
their safe defaults:

```dotenv
MULTI_LANE_CAPITAL_ENABLED=0
MULTI_LANE_CAPITAL_STRATEGY=
```

No exchange credentials are required for the default public-data measurement
runtime.

## Start the default stack

```bash
export VNEDGE_BUILD_SHA="$(git rev-parse --short HEAD)"
docker compose config -q
docker compose up -d --build multi-lane-shadow dashboard-tls
docker compose ps
docker compose logs -f multi-lane-shadow
```

The default service builds SHADOW measurement lanes and exposes the read-only
dashboard. It does not submit venue orders.

Both application and edge health are explicit:

- `GET /health` and `GET /healthz` are public liveness-only responses;
- Compose checks the application directly and checks the Caddy TLS listener;
- `/ready` remains the snapshot-readiness probe and may return 503 during warmup.

## Optional continuous research

The research loop is opt-in and remains evidence-only:

```bash
docker compose --profile research up -d research-loop
```

It may ingest public candles and funding data and publish walk-forward evidence.
It cannot change the capital roster, promote a strategy, or route orders.

## Paper-capital evaluation is frozen

The reviewed `CAPITAL_APPROVED` set is currently empty. Registration, a past
paper trial, or absence from the killed set is not permission. Supplying the
paper-capital environment gates therefore fails closed until a strategy is
promoted through a reviewed code change containing its pre-registered OOS and
paper evidence.

## Dashboard access

Prefer an SSH tunnel to the loopback-bound application port:

```bash
ssh -N -L 8080:127.0.0.1:8080 user@host
```

Then open `http://127.0.0.1:8080/?token=<DASHBOARD_TOKEN>`. The optional Caddy
service can expose TLS on port 8765; configure `DASHBOARD_ALLOWLIST` before
making it internet-reachable.

The IP-based `:8765` configuration uses a self-signed certificate and is not a
production-trusted browser endpoint. Verify it with the explicit public
certificate as a trust anchor—never by disabling certificate validation:

```bash
curl --cacert cert.pem https://HOST:8765/healthz
```

For production browser access, configure a DNS name and ACME certificate first;
enable HSTS only after the valid-certificate path is serving reliably.

## Verification and shutdown

```bash
docker compose ps
docker compose logs --tail=200 multi-lane-shadow
docker compose down
```

There is no live trading service in this Compose file. Adding one is an
architecture and security change, not an environment toggle.
## Fleet policy verification

After every image recreate, verify the running snapshot before treating the
deployment as healthy:

```bash
docker compose exec multi-lane-shadow \
  python -m vnedge.runtime.fleet_policy \
  --url http://127.0.0.1:8080/state \
  --expected-build-sha "$VNEDGE_BUILD_SHA"
```

The command is read-only and exits non-zero if the build differs, live trading
is enabled, any paper/live lane uses a killed or non-approved strategy, or the
declared capital roster cannot be audited. The current approved-capital set is
empty, so the expected production result is `safe: true` with measurement-only
shadow lanes.
