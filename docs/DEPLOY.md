# Deploying VNEDGE (paper trial) to a Linux VPS

Target per the project charter: Ubuntu VPS + Docker. Everything below is for
the PAPER trial — no API keys, no live orders (the manifest loader refuses
them structurally).

## 1. VPS prep (once)

```bash
# Ubuntu 22.04+
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # re-login after this
```

Recommended: AWS Mumbai / any region; latency barely matters for 1h-bar paper
trading. 1 vCPU / 1–2 GB RAM is plenty.

## 2. Ship the repo + trial state

```bash
# from the Mac — code. Excludes MUST be root-anchored (leading /):
# a bare "data" would also exclude src/vnedge/data and break the install.
rsync -av --exclude /.venv --exclude /data --exclude /logs \
    --exclude /models --exclude /.git \
    ~/Desktop/VNEDGE/ vps:~/vnedge/

# trial state (REQUIRED to continue the current trial, not restart it):
# stop the local process FIRST so the account snapshot is final
kill <local-trial-pid>
rsync -av ~/Desktop/VNEDGE/logs/paper_trials/ vps:~/vnedge/logs/paper_trials/
rsync -av ~/Desktop/VNEDGE/research/paper_trials/ vps:~/vnedge/research/paper_trials/
```

The account store (`logs/paper_trials/<trial>.account.json`) is what makes
this a CONTINUATION: on start, the runner logs `resumed: true` with the
carried balance/positions/loss-streak. Never run the same trial on two
machines at once.

## 3. Configure and start

```bash
cd ~/vnedge
cat > .env <<EOF
DASHBOARD_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
TELEGRAM_BOT_TOKEN=            # optional: @BotFather token
TELEGRAM_CHAT_ID=              # optional: your chat id
EOF
docker compose up -d --build
docker compose logs -f paper-trial   # watch the resume line
```

`restart: unless-stopped` + the account store = reboots continue the trial.

## 4. Dashboard access (never public)

The container port is mapped to the VPS loopback only. From the Mac:

```bash
ssh -N -L 8080:127.0.0.1:8080 vps
open "http://127.0.0.1:8080/?token=<DASHBOARD_TOKEN from vps .env>"
```

## 5. Daily checks (mostly automated now)

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
