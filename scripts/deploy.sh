#!/usr/bin/env bash
# VNEDGE deploy — serialized, provenance-safe.
#
# Two concurrent `docker compose up` invocations SIGKILLed the whole stack on
# 2026-07-07 (trial lanes down ~30min with an open position). This script is
# the ONLY sanctioned deploy path: it takes an exclusive lock, refuses dirty
# trees, resets to origin/main, builds THEN recreates (never both at once, to
# avoid the 2026-07-11 swap-thrash), and verifies lanes resume.
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

PREV=$(git rev-parse HEAD)
git fetch --prune origin
git reset --hard origin/main
HEAD_SHA=$(git rev-parse HEAD)
echo "deploying $(git rev-parse --short HEAD)"

# Build the image ONCE, up front (not interleaved with recreation). On a box
# already running ~24 containers, `up -d --build` rebuilt AND recreated the
# whole fleet simultaneously and thrashed the VM into swap (2026-07-11, ~10min
# of SSH/TLS starvation). Separating build from recreate keeps memory bounded.
# Skip the build entirely when no tracked file that lands in the image changed.
NEED_BUILD=1
if [ "$PREV" != "$HEAD_SHA" ] && git diff --quiet "$PREV" "$HEAD_SHA" -- \
        src/ pyproject.toml Dockerfile 2>/dev/null; then
    NEED_BUILD=0
    echo "no image-affecting changes since ${PREV:0:7} — skipping rebuild"
fi
if [ "$NEED_BUILD" = 1 ]; then
    echo "building image (isolated from recreation)..."
    docker compose build
fi

# Recreate from the already-built image. --no-build guarantees no build spike
# here; Compose still only recreates services whose config/image changed.
docker compose up -d --no-build

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
