#!/usr/bin/env bash
# VNEDGE deploy — serialized, provenance-safe.
#
# Two concurrent `docker compose up` invocations SIGKILLed the whole stack on
# 2026-07-07 (trial lanes down ~30min with an open position). This script is
# the ONLY sanctioned deploy path: it takes an exclusive lock, refuses dirty
# trees, resets to origin/main, rebuilds, and verifies lanes resume.
set -euo pipefail
cd "$(dirname "$0")/.."

exec 9>/tmp/vnedge-deploy.lock
if ! flock -n 9; then
    echo "another deploy is in progress (holder of /tmp/vnedge-deploy.lock) — aborting" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "working tree is DIRTY — commit/stash first; deploys run from committed code only:" >&2
    git status --porcelain | head -5 >&2
    exit 1
fi

git fetch --prune origin
git reset --hard origin/main
echo "deploying $(git rev-parse --short HEAD)"

docker compose up -d --build

echo "waiting for lanes..."
for _ in $(seq 1 60); do
    if docker compose logs multi-lane-shadow 2>/dev/null | grep -q "lanes running"; then
        docker compose logs multi-lane-shadow 2>&1 | grep "lanes running" | tail -1
        exit 0
    fi
    sleep 5
done
echo "lanes did not report running within 5 minutes — investigate" >&2
exit 1
