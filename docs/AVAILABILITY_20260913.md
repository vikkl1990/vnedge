# Runtime availability hardening — 2026-09-13

Scope: process availability, truthful display freshness, bounded recovery.
No strategy, cost profile, capital permission, transport cutover or parity
threshold changes. A single VM cannot guarantee uninterrupted availability
through host/network failure or replacement of the dashboard process.

## Changes

- Soft operations warnings no longer overwrite setup/holding state. Hard
  entry blocks remain in Ops health and readiness; open positions stay visible.
- A recent, timezone-aware, non-backfill evaluation from the running lane
  supplements the bounded WAL-tail audit. Missing/stale journals still fail.
  This prevents busy diagnostics pushing an hourly evaluation out of the tail
  and falsely reporting SILENT. Offline audits retain journal-only semantics.
- Runtime journal health audits run on a dedicated single background worker,
  not the HTTP/scanner event loop. Initial audit pending remains unready.
- Dashboard read requests time out after 20 seconds so polling can retry.
  WebSocket silence triggers reconnection, old/future frames are rejected,
  and the global strip visibly marks stale snapshots. This is not live-trading
  permission. Browser sleep suspends execution; resume refreshes immediately.
- Failed deployment no longer tears down the whole Compose stack.
- A host systemd timer inspects already-deployed services every minute,
  under the SAME deployment lock. Docker restart policies handle process
  exits; the watchdog addresses persistent unhealthy running containers.

## Recovery policy

`scripts/service_watchdog.py` is read-only unless `--apply` is supplied.
It has no venue credentials or trading API. Three unhealthy checks plus ten
minutes uptime are required. At most one container restart per check, at
least 15 minutes between attempts, and at most two attempts per service/hour.
The attempt is persisted BEFORE restart. Exhaustion requires operator action.
It never tests `/ready` to decide to restart: no signals, market denial,
missing parity, incomplete research or a kill switch are NOT process failures.
It never restarts stopped/missing containers, runs `compose up`, clears KILL,
resets reconciliation, or enables a profile. Legacy gap/vision/book writers
are excluded. A healthcheck-free worker is honestly `unprobed`, not healthy.

Host state/report: `data/reports/service_watchdog.json` (atomic), events in
`journalctl -u vnedge-watchdog.service`. Units are configured for the documented
Ubuntu VM at `/home/ubuntu/vnedge`; edit the user/path for another host.

Install after committed deployment:

```sh
sudo install -d -o ubuntu -g ubuntu -m 755 /home/ubuntu/vnedge/data/reports
sudo install -m 644 deploy/vnedge-watchdog.service /etc/systemd/system/vnedge-watchdog.service
sudo install -m 644 deploy/vnedge-watchdog.timer /etc/systemd/system/vnedge-watchdog.timer
sudo systemctl daemon-reload
sudo systemctl enable --now vnedge-watchdog.timer
sudo systemctl start vnedge-watchdog.service
```

The host user must be able to atomically replace the report in that directory.
Container-created directories may be root-owned; set ownership on the reports
directory only, never recursively change existing research artifacts.

Before deliberate maintenance, stop the timer; resume it afterwards.
`sudo systemctl disable --now vnedge-watchdog.timer` disables automatic recovery.
Failures are recorded, not hidden. External host-down alerting and redundant
hosting remain separate work; this watchdog cannot run when its VM is down.

## Remaining performance/evidence work

The measured 15m p95 receipt (roughly 5 seconds) and compute (roughly 1.9
seconds) remain soft warnings. Budgets are unchanged; restart is not a latency
optimization. Do not turn these green by widening limits. Canonical transport
parity, execution parity and live permissions remain independently blocked.
