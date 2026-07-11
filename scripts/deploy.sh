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

DEPLOY_START=$(date +%s)
PREV=$(git rev-parse HEAD)
git fetch --prune origin
git reset --hard origin/main
HEAD_SHA=$(git rev-parse HEAD)
echo "deploying $(git rev-parse --short HEAD)"

# Build the image ONCE, up front (not interleaved with recreation). On a box
# already running ~24 containers, `up -d --build` rebuilt AND recreated the
# whole fleet simultaneously and thrashed the VM into swap (2026-07-11, ~10min
# of SSH/TLS starvation). Separating build from recreate keeps memory bounded.
# Skip the build only when NOTHING that lands in the image changed. The path
# list must include EVERY input to the image: a docs/ or .dockerignore change
# once shipped nothing because it was omitted here (2026-07-11).
NEED_BUILD=1
if [ "$PREV" != "$HEAD_SHA" ] && git diff --quiet "$PREV" "$HEAD_SHA" -- \
        src/ research/ docs/ pyproject.toml README.md Dockerfile \
        .dockerignore docker-compose.yml 2>/dev/null; then
    NEED_BUILD=0
    echo "no image-affecting changes since ${PREV:0:7} — skipping rebuild"
fi
if [ "$NEED_BUILD" = 1 ]; then
    echo "building image (isolated from recreation)..."
    # Explicit build so a failure aborts the deploy loudly (set -e); a silent
    # build failure once left a stale image serving while the deploy "passed".
    if ! docker compose build; then
        echo "IMAGE BUILD FAILED — aborting deploy, nothing recreated" >&2
        exit 1
    fi
fi

# Recreate from the already-built image. --no-build guarantees no build spike
# here; Compose still only recreates services whose config/image changed.
docker compose up -d --no-build

echo "waiting for lanes..."
LANES_OK=0
for _ in $(seq 1 60); do
    # --since DEPLOY_START so we read THIS deploy's container, not a stale
    # container's historical "lanes running" line (that false-positive is how
    # a failed deploy read green on 2026-07-11).
    if docker compose logs --since "$DEPLOY_START" multi-lane-shadow 2>/dev/null \
            | grep -q "lanes running"; then
        docker compose logs --since "$DEPLOY_START" multi-lane-shadow 2>&1 \
            | grep "lanes running" | tail -1
        LANES_OK=1
        break
    fi
    sleep 5
done
if [ "$LANES_OK" != 1 ]; then
    echo "lanes did not report running within 5 minutes — investigate" >&2
    exit 1
fi

# Freshness assertion: if we built, the running container MUST have been
# recreated since the deploy began. A stale StartedAt means the new image
# never took (build skipped by compose, or a partial recreate) — fail loudly.
if [ "$NEED_BUILD" = 1 ]; then
    cid=$(docker compose ps -q multi-lane-shadow)
    started=$(docker inspect --format '{{.State.StartedAt}}' "$cid")
    started_epoch=$(date -d "$started" +%s 2>/dev/null || echo 0)
    if [ "$started_epoch" -lt "$DEPLOY_START" ]; then
        echo "STALE IMAGE: multi-lane-shadow was not recreated (started $started," \
             "before this deploy) — the new image did not take" >&2
        exit 1
    fi
    echo "freshness OK: container recreated at $started"
fi
exit 0
