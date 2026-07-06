# Deploying VNEDGE (multi-lane shadow) to a Linux VPS

Target per the project charter: Ubuntu VPS + Docker. Everything below is for
the live-data shadow workspace — no API keys, no live orders. Each lane runs
through sizing, the pre-trade risk gateway, journaling, reconciliation, and
dashboard telemetry, but shadow mode never submits to a broker.

## 1. VPS prep (once)

```bash
# Ubuntu 22.04+
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # re-login after this
```

Recommended: AWS Mumbai / any region; latency barely matters for 1h-bar paper
trading. 1 vCPU / 1–2 GB RAM is plenty.

## 2. Deploy code with git provenance

Deploy from a real git checkout on the VPS. Do not run production/shadow
services from an rsynced tree without `.git`; that removes commit attribution,
rollback, and a reliable `git rev-parse HEAD`.

First install:

```bash
git clone https://github.com/vikkl1990/vnedge.git ~/vnedge
cd ~/vnedge
git checkout main
git pull --ff-only
git rev-parse HEAD
```

Update an existing deploy:

```bash
cd ~/vnedge
git fetch --prune origin
git checkout main
git reset --hard origin/main
git clean -fd --exclude=.env --exclude=data --exclude=logs \
    --exclude=research/paper_trials --exclude=research/live_research \
    --exclude=deploy/certs
git rev-parse HEAD
```

Before restarting services, record the printed commit in the deploy log or
operator notes. Rollback is the inverse: `git fetch`, then
`git reset --hard <known-good-commit>`, followed by `docker compose up -d --build`.

## 3. Ship trial/runtime state

```bash
# trial state (REQUIRED to continue the current trial, not restart it):
# stop the local process FIRST so the account snapshot is final
kill <local-trial-pid>
rsync -av ~/Desktop/VNEDGE/logs/paper_trials/ vps:~/vnedge/logs/paper_trials/
rsync -av ~/Desktop/VNEDGE/research/paper_trials/ vps:~/vnedge/research/paper_trials/
```

The account store (`logs/paper_trials/<lane>.account.json`) is what makes
this a CONTINUATION for paper lanes and a stable audit trail for shadow
lanes. Never run the same lane id on two machines at once.

Live research files under `research/live_research/` are generated runtime
state, not source code. Preserve or archive them when they are evidence, but do
not commit them.

## 4. Configure and start

```bash
cd ~/vnedge
cat > .env <<EOF
DASHBOARD_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
TELEGRAM_BOT_TOKEN=            # optional: @BotFather token
TELEGRAM_CHAT_ID=              # optional: your chat id
# optional grid expansion; defaults shown
MULTI_LANE_EXCHANGES=binanceusdm,bybit
MULTI_LANE_SYMBOLS=BTC/USDT:USDT
EOF
docker compose up -d --build
docker compose logs -f multi-lane-shadow   # watch lane build/resume lines
```

`restart: unless-stopped` + per-lane account stores = reboots continue the
same governed workspace.

## 5. Dashboard access (never public)

The container port is mapped to the VPS loopback only. From the Mac:

```bash
ssh -N -L 8080:127.0.0.1:8080 vps
open "http://127.0.0.1:8080/?token=<DASHBOARD_TOKEN from vps .env>"
```

## 6. Daily checks (mostly automated now)

Telegram (if configured) pushes: fills, stale feed, kill switch, journal
failures, daily-loss stop, loss streaks, drawdown-envelope breaches.
Manual fallback:

```bash
docker compose ps
tail vnedge/logs/paper_trials/funding_mr_btc_v1_20260703.alerts.jsonl
cat  vnedge/logs/paper_trials/funding_mr_btc_v1_20260703.account.json
```

Emergency halt (entries stop, exits still allowed):

```bash
touch vnedge/logs/paper_trials/funding_mr_btc_v1_20260703.KILL
```

## Notes

- The image has NOT been built on the dev Mac (no Docker there) — first
  `docker compose build` on the VPS is the validation step; all Python deps
  ship manylinux wheels for 3.12-slim.
- systemd alternative (no Docker): copy the repo, create a venv, and wrap
  `python -m vnedge.runtime.paper_trial ... --hours 720 --dashboard` in a
  unit with `Restart=always` — the account store makes restarts safe either way.
