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
docker compose config -q
docker compose up -d --build multi-lane-shadow dashboard-tls
docker compose ps
docker compose logs -f multi-lane-shadow
```

The default service builds SHADOW measurement lanes and exposes the read-only
dashboard. It does not submit venue orders.

## Optional continuous research

The research loop is opt-in and remains evidence-only:

```bash
docker compose --profile research up -d research-loop
```

It may ingest public candles and funding data and publish walk-forward evidence.
It cannot change the capital roster, promote a strategy, or route orders.

## Optional paper-capital evaluation

Paper capital is deliberately not a deployment default. To evaluate one
registered eligible strategy with simulated fills, set both gates:

```dotenv
MULTI_LANE_CAPITAL_ENABLED=1
MULTI_LANE_CAPITAL_STRATEGY=trend_continuation_v1
```

Restart `multi-lane-shadow` after the change. Unknown, research-only, or killed
strategy IDs fail closed. Clear both values to return to measurement-only mode.

## Dashboard access

Prefer an SSH tunnel to the loopback-bound application port:

```bash
ssh -N -L 8080:127.0.0.1:8080 user@host
```

Then open `http://127.0.0.1:8080/?token=<DASHBOARD_TOKEN>`. The optional Caddy
service can expose TLS on port 8765; configure `DASHBOARD_ALLOWLIST` before
making it internet-reachable.

## Verification and shutdown

```bash
docker compose ps
docker compose logs --tail=200 multi-lane-shadow
docker compose down
```

There is no live trading service in this Compose file. Adding one is an
architecture and security change, not an environment toggle.
