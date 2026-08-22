# Deploying VNEDGE

VNEDGE now deploys as a measurement-first, no-live-order stack. Docker Compose
contains the measurement dashboard runtime, the public-trade Pulse recorder,
an optional research loop, and the TLS proxy.

## Required configuration

Create `.env` from `.env.example` and set a strong dashboard token:

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Copy the generated value into `DASHBOARD_TOKEN` in `.env`. Leave the capital
controls at their safe defaults:

```dotenv
DASHBOARD_TOKEN=<generated value>
MULTI_LANE_CAPITAL_ENABLED=0
MULTI_LANE_CAPITAL_STRATEGY=
```

No exchange credentials are required for the default public-data measurement
runtime.

## Start the default stack

```bash
export VNEDGE_BUILD_SHA="$(git rev-parse --short HEAD)"
docker compose config -q
docker compose up -d --build multi-lane-shadow pulse-recorder dashboard-tls
docker compose ps
docker compose logs -f multi-lane-shadow
```

`multi-lane-shadow` starts through `vnedge.runtime.scanner_startup`. On every
container start—including Docker daemon and host restarts—it downloads at least nine
complete Binance Vision aggTrade days for BTC/ETH, deterministically rebuilds
the canonical 1m→4h ladder, and fills the unpublished closed 5m tail from
the recent aggregate-trade API. Existing archive days and canonical minutes
are skipped, so ordinary restarts fetch only the missing delta. A failed
continuity proof prevents the lane runtime from starting; the container's
restart policy retries the complete fail-closed entrypoint. It never falls
back to exchange OHLCV as exact VWAP history.

The final proof is persisted at
`data/reports/scanner_prerequisites.json`. It verifies current contiguous
exact-volume 5m/15m/1h/4h tails for BTC and ETH. Treat a non-zero prerequisite
exit as a real data-integrity failure; do not bypass the dependency.

The default services build SHADOW measurement lanes, record public trades for
the canonical Pulse candle lake, and expose the dashboard. They do not submit
venue orders.

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

## Optional SHADOW_OBSERVE drill

`structure_bos_1h` may be run on public, closed 1h bars as a virtual-only
observation lane. This is a separate permission from capital approval: it may
journal a `shadow_intent` and resolve a `shadow_outcome`, but it cannot submit
an order. Keep capital disabled and set both observe gates:

```dotenv
MULTI_LANE_CAPITAL_ENABLED=0
MULTI_LANE_CAPITAL_STRATEGY=
MULTI_LANE_SHADOW_OBSERVE_ENABLED=1
MULTI_LANE_SHADOW_OBSERVE_STRATEGY=structure_bos_1h
MULTI_LANE_SHADOW_OBSERVE_EXCHANGE=binanceusdm
MULTI_LANE_SHADOW_OBSERVE_SYMBOL=BTC/USDT:USDT
MULTI_LANE_SHADOW_OBSERVE_TIMEFRAME=1h
```

Preferred multi-horizon observer deployment (replaces the singleton variables
above; do not set both):

```bash
MULTI_LANE_SHADOW_OBSERVE_ENABLED=0
MULTI_LANE_SHADOW_OBSERVE_STRATEGY=
MULTI_LANE_SHADOW_OBSERVE_ROSTER_PATH=config/shadow-observers.v1.json
```

The checked-in v1 roster runs virtual-only squeeze acceptance at 5m and
range-expansion + BoS observation at 1h for BTC and ETH. It does not add a
paper lane, change `CAPITAL_APPROVED`, or enable live orders. Startup fails on
unknown fields, wrong timeframes, duplicate stable lane IDs, or an ineligible
strategy.

Before switching from legacy lane IDs to the versioned roster, inspect stale
artifacts without changing them:

```bash
python -m vnedge.runtime.orphan_lane_archive \
  --journal-dir logs/paper_trials
```

After confirming the listed lane IDs are absent from the desired roster, rerun
with `--apply`. Files are moved—not deleted—to
`logs/paper_trials/archive/orphans/<UTC timestamp>/` with a recovery manifest.
Stop `multi-lane-shadow` before applying the move, inspect the manifest, and
only then recreate the service. A partial filesystem failure leaves a
`status: failed` manifest listing every file already moved, so restoration is
explicit and no evidence is silently lost.

After recreation, a legacy singleton must report `shadow_observe_lanes: 1`;
the checked-in versioned roster must report `shadow_observe_lanes: 6` and
`shadow_observe_timeframes: ["1h", "5m"]`. Both must report
`paper_lanes: 0`, `orders_allowed: false`, and `live_orders_allowed: false`.
The Desk banner must say
`SHADOW_OBSERVE · virtual only`. A bad, killed, missing, or wrong-timeframe
strategy fails process startup.

For the automatic BTC/ETH fee-wall measurement drill, keep the same capital
gates off and use two virtual-only 5-minute lanes:

```dotenv
MULTI_LANE_CAPITAL_ENABLED=0
MULTI_LANE_CAPITAL_STRATEGY=
MULTI_LANE_SHADOW_OBSERVE_ENABLED=1
MULTI_LANE_SHADOW_OBSERVE_STRATEGY=fee_wall_momentum_observer_v1
MULTI_LANE_SHADOW_OBSERVE_EXCHANGE=binanceusdm
MULTI_LANE_SHADOW_OBSERVE_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT
MULTI_LANE_SHADOW_OBSERVE_TIMEFRAME=5m
```

This journals virtual crossing intents and their frozen SL/TP outcomes. It does
not place paper or live orders; `/state.runtime_control` must continue to show
`paper_lanes: 0`, `orders_allowed: false`, and `live_orders_allowed: false`.

To run the current exact-volume quote-acceptance candidate instead, change only
the strategy id:

```dotenv
MULTI_LANE_SHADOW_OBSERVE_STRATEGY=squeeze_expansion_breakout_v4
```

V4 is still `RESEARCH_ONLY`. It journals current bid/ask acceptance and virtual
outcomes, but does not create an `OrderIntent`. Its exploratory 1-minute proxy
replay remains V3 evidence and is not V4 tick-parity or promotion evidence.

## Dashboard access

Prefer an SSH tunnel to the loopback-bound application port:

```bash
ssh -N -L 8080:127.0.0.1:8080 user@host
```

Then open `http://127.0.0.1:8080/app/` and enter `DASHBOARD_TOKEN` on the sign-in
screen. The URL remains non-secret. For direct HTTP tunnel access set
`DASHBOARD_COOKIE_SECURE=false`; keep the production default `true` for HTTPS.
The optional Caddy service can expose TLS on port 8765; configure
`DASHBOARD_ALLOWLIST` before making it internet-reachable.

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
