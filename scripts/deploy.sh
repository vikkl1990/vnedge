#!/usr/bin/env bash
# VNEDGE deploy — serialized, provenance-safe.
#
# Two concurrent `docker compose up` invocations SIGKILLed the whole stack on
# 2026-07-07 (trial lanes down ~30min with an open position). This script is
# the ONLY sanctioned deploy path: it takes an exclusive lock, refuses dirty
# trees, resets to origin/main, builds THEN recreates (never both at once, to
# avoid the 2026-07-11 swap-thrash), and verifies lanes resume.
set -euo pipefail

# Read the whole body into memory before running it: `git reset` below can
# rewrite THIS file mid-deploy, and bash reads scripts lazily — a brace
# group forces a full parse first, so no old/new line mixing (2026-07-11).
{
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

# Compose services that write mounted research artifacts must use the host
# deploy user's UID/GID. OCI Ubuntu images can be 1001 rather than 1000, so
# detect it here instead of baking in a default that may poison the worktree.
export VNEDGE_BUILD_SHA="${VNEDGE_BUILD_SHA:-$HEAD_SHA}"
export VNEDGE_HOST="${VNEDGE_HOST:-$(hostname)}"
export VNEDGE_CONTAINER_UID="${VNEDGE_CONTAINER_UID:-$(id -u)}"
export VNEDGE_CONTAINER_GID="${VNEDGE_CONTAINER_GID:-$(id -g)}"
echo "compose artifact writer uid/gid: ${VNEDGE_CONTAINER_UID}:${VNEDGE_CONTAINER_GID}"

# Build the image ONCE, up front (not interleaved with recreation). On a box
# already running ~24 containers, `up -d --build` rebuilt AND recreated the
# whole fleet simultaneously and thrashed the VM into swap (2026-07-11, ~10min
# of SSH/TLS starvation). Separating build from recreate keeps memory bounded.
# Skip the build only when NOTHING that lands in the image changed. The path
# list must include EVERY input to the image: a docs/ or .dockerignore change
# once shipped nothing because it was omitted here (2026-07-11).
# The deploy script is included because every committed deploy must stamp the
# exact serving revision into /app/BUILD_SHA.  Otherwise a deploy-only change
# skips the build and the later fleet-policy SHA assertion can never pass.
IMAGE_INPUTS="src/ research/ docs/ frontend/ scripts/deploy.sh pyproject.toml README.md Dockerfile .dockerignore docker-compose.yml"
APP_BUILD_SERVICE=multi-lane-shadow
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}"
APP_BUILD_IMAGE="${COMPOSE_PROJECT}-${APP_BUILD_SERVICE}:latest"

service_image_id() {
    local svc="$1"
    local cid
    cid=$(docker compose ps -q "$svc" 2>/dev/null | head -1 || true)
    if [ -n "$cid" ]; then
        docker inspect --format '{{.Image}}' "$cid" 2>/dev/null || true
        return
    fi
    docker image inspect --format '{{.Id}}' "${COMPOSE_PROJECT}-${svc}:latest" 2>/dev/null || true
}

NEED_BUILD=1
if [ "$PREV" != "$HEAD_SHA" ] && git diff --quiet "$PREV" "$HEAD_SHA" -- $IMAGE_INPUTS 2>/dev/null; then
    NEED_BUILD=0
    echo "no image-affecting changes since ${PREV:0:7} — skipping rebuild"
fi

# CONTENT-BASED stale-image guard (2026-07-14): git-HEAD diffing is not a
# reliable proxy for what is IN the running image — a prior deploy that skipped
# the build (or a manual `up -d --no-build`) leaves the image OLDER than the
# committed code while NEED_BUILD reads 0. #149's code sat un-deployed for a day
# exactly this way. So: if the running image is older than the newest commit to
# any image input, force a rebuild regardless of the git diff.
run_img=$(service_image_id "$APP_BUILD_SERVICE")
if [ -n "$run_img" ]; then
    img_epoch=$(date -d "$(docker inspect --format '{{.Created}}' "$run_img")" +%s 2>/dev/null || echo 0)
    code_epoch=$(git log -1 --format=%ct -- $IMAGE_INPUTS 2>/dev/null || echo 0)
    if [ "$code_epoch" -gt "$img_epoch" ]; then
        NEED_BUILD=1
        echo "running image predates committed code (img $img_epoch < code $code_epoch) — forcing rebuild"
    fi
else
    NEED_BUILD=1
    echo "no inspectable running image for multi-lane-shadow — forcing rebuild"
fi
if [ "$NEED_BUILD" = 1 ]; then
    echo "building image (isolated from recreation)..."
    # Explicit build so a failure aborts the deploy loudly (set -e); a silent
    # build failure once left a stale image serving while the deploy "passed".
    # Compose treats every `build: .` service as a separate export target even
    # though they all run the same app image with different commands. Exporting
    # 20+ identical images in one BuildKit bake has wedged this VM in futex
    # waits. Build the canonical app service once, then tag that image for the
    # sibling app services so `up --no-build` can recreate from local images.
    if ! docker compose build --build-arg VNEDGE_BUILD_SHA="$HEAD_SHA" "$APP_BUILD_SERVICE"; then
        echo "IMAGE BUILD FAILED — aborting deploy, nothing recreated" >&2
        exit 1
    fi
    # Tag ALL services' images (force every profile), so the `research` fleet can
    # be opted in later (COMPOSE_PROFILES=research) without a rebuild. Only the
    # RECREATE below is profile-scoped — tagging must cover everything.
    for svc in $(COMPOSE_PROFILES=research docker compose config --services); do
        case "$svc" in
            "$APP_BUILD_SERVICE"|dashboard-tls) continue ;;
        esac
        docker tag "$APP_BUILD_IMAGE" "${COMPOSE_PROJECT}-${svc}:latest"
    done
fi

# Recreate from the already-built image. --no-build guarantees no build spike
# here; Compose still only recreates services whose config/image changed.
#
# WAVED recreate (retained from the former large fleet): a source change rebuilds
# the shared image and can recreate every app service together. The canonical
# recorder must restore its exact tick delta BEFORE newly recreated scanner
# lanes start. Starting lanes first caused a deterministic restart race: the
# venue delivered a close while pulse-recorder was still restoring, the row
# missed the lane's eight-second deadline, and that lane stayed reduce-only
# until another clean bar. The old scanner can remain online while the recorder
# restarts; it is replaced only after the recorder logs that restoration is
# complete. Remaining services are still batched to cap memory pressure.
# On a daemon race (name-in-use mid-recreate — took the fleet down 2026-07-31),
# self-heal once with down --remove-orphans, then fall back to a plain up.
recreate_in_waves() {
    local wave="${DEPLOY_WAVE_SIZE:-6}" pause="${DEPLOY_WAVE_PAUSE:-25}"
    if [ "$NEED_BUILD" = 1 ]; then
        docker compose up -d --no-build pulse-recorder || return 1
        local recorder_ready=0
        for _ in $(seq 1 "${DEPLOY_RECORDER_WAIT_ATTEMPTS:-120}"); do
            if docker compose logs --since "$DEPLOY_START" pulse-recorder 2>/dev/null \
                    | grep -q "tick recorder:"; then
                recorder_ready=1
                break
            fi
            sleep 2
        done
        if [ "$recorder_ready" != 1 ]; then
            echo "canonical recorder did not finish restart restoration" >&2
            return 1
        fi
        echo "canonical recorder restored before lane recreation"
    fi
    docker compose up -d --no-build multi-lane-shadow || return 1
    sleep "$pause"
    local rest svc
    local -a batch=()
    rest=$(docker compose config --services 2>/dev/null \
        | grep -vE '^(multi-lane-shadow|pulse-recorder)$')
    for svc in $rest; do
        batch+=("$svc")
        if [ "${#batch[@]}" -ge "$wave" ]; then
            docker compose up -d --no-build "${batch[@]}" || return 1
            batch=()
            sleep "$pause"
        fi
    done
    if [ "${#batch[@]}" -gt 0 ]; then
        docker compose up -d --no-build "${batch[@]}" || return 1
    fi
    return 0
}
if ! recreate_in_waves; then
    echo "waved recreate raced — self-healing: down --remove-orphans + retry" >&2
    docker compose down --remove-orphans || true
    recreate_in_waves || docker compose up -d --no-build
fi

echo "waiting for lanes..."
LANES_OK=0
# A clean volume may spend substantial time in the exact aggTrade prerequisite
# entrypoint before the HTTP runtime exists. Existing volumes are delta-only
# and return quickly; retain a bounded one-hour cold-start allowance.
for _ in $(seq 1 "${DEPLOY_LANE_WAIT_ATTEMPTS:-720}"); do
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
    # No FRESH "lanes running" line. If we didn't rebuild, nothing was
    # recreated, so the absence just means the already-running lanes weren't
    # restarted — confirm they're still up rather than false-failing.
    if [ "$NEED_BUILD" = 0 ] \
        && docker inspect --format '{{.State.Running}}' \
            "$(docker compose ps -q multi-lane-shadow)" 2>/dev/null | grep -q true; then
        echo "no rebuild; existing lanes still running (not restarted)"
    else
        echo "lanes did not report running within 5 minutes — investigate" >&2
        exit 1
    fi
fi

# Freshness assertion: if we built, the running container must (a) have been
# recreated since the deploy began, AND (b) run an image NEWER than the code —
# the content check that git-time-diffing alone missed on 2026-07-14.
if [ "$NEED_BUILD" = 1 ]; then
    cid=$(docker compose ps -q multi-lane-shadow)
    started=$(docker inspect --format '{{.State.StartedAt}}' "$cid")
    started_epoch=$(date -d "$started" +%s 2>/dev/null || echo 0)
    if [ "$started_epoch" -lt "$DEPLOY_START" ]; then
        echo "STALE IMAGE: multi-lane-shadow was not recreated (started $started," \
             "before this deploy) — the new image did not take" >&2
        exit 1
    fi
    new_img=$(service_image_id "$APP_BUILD_SERVICE")
    if [ -z "$new_img" ]; then
        echo "STALE IMAGE: no inspectable image for $APP_BUILD_SERVICE after deploy" >&2
        exit 1
    fi
    new_img_epoch=$(date -d "$(docker inspect --format '{{.Created}}' "$new_img")" +%s 2>/dev/null || echo 0)
    final_code_epoch=$(git log -1 --format=%ct -- $IMAGE_INPUTS 2>/dev/null || echo 0)
    if [ "$new_img_epoch" -lt "$final_code_epoch" ]; then
        echo "STALE IMAGE: running image ($new_img_epoch) still older than committed" \
             "code ($final_code_epoch) after build — build did not take" >&2
        exit 1
    fi
    echo "freshness OK: container recreated at $started, image newer than code"
fi

# Edge assertion: the serving process can be healthy while Caddy is missing,
# misconfigured, or unable to reach its upstream. The proxy healthcheck hits
# the real TLS listener and /healthz; make that a deployment gate.
echo "waiting for TLS edge health..."
EDGE_CID=$(docker compose ps -q dashboard-tls)
if [ -z "$EDGE_CID" ]; then
    echo "TLS EDGE FAILED — dashboard-tls container is absent" >&2
    exit 1
fi
EDGE_OK=0
for _ in $(seq 1 30); do
    edge_health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$EDGE_CID" 2>/dev/null || echo missing)
    if [ "$edge_health" = "healthy" ]; then
        EDGE_OK=1
        break
    fi
    if [ "$edge_health" = "unhealthy" ] || [ "$edge_health" = "missing" ]; then
        echo "TLS edge health is $edge_health" >&2
        break
    fi
    sleep 2
done
if [ "$EDGE_OK" != 1 ]; then
    echo "TLS EDGE FAILED — /healthz did not become healthy" >&2
    docker compose logs --tail=40 dashboard-tls >&2 || true
    exit 1
fi
echo "TLS edge health OK"

# Policy assertion: process health and image freshness are not enough. Refuse a
# deploy that came up with live enabled, a killed/unapproved strategy in a
# paper/live lane, or a roster count the snapshot does not expose for audit.
echo "verifying deployed capital policy..."
if ! docker compose exec -T multi-lane-shadow \
        python -m vnedge.runtime.fleet_policy \
        --url http://127.0.0.1:8080/state \
        --expected-build-sha "$HEAD_SHA"; then
    echo "FLEET POLICY FAILED — deployment is running but unsafe; investigate immediately" >&2
    exit 1
fi
echo "fleet policy OK: measurement-only posture confirmed"

# Operational readiness assertion: a process can publish "lanes running" and
# still lose individual lane tasks immediately afterwards, or remain arm-
# blocked while the prerequisite worker repairs a candle hole. Wait through
# bounded recovery and require the serving runtime's aggregate /ready proof.
echo "waiting for operational readiness..."
READY_OK=0
READY_PAYLOAD=""
for _ in $(seq 1 "${DEPLOY_READY_WAIT_ATTEMPTS:-720}"); do
    if READY_PAYLOAD=$(docker compose exec -T multi-lane-shadow python -c '
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/ready", timeout=5) as response:
    payload = json.load(response)
print(json.dumps(payload, separators=(",", ":")))
# /ready publishes the readiness band as ``status``.  Retain support for the
# former boolean shape so a rolling deploy can validate either side of the
# API transition without waiting through the full recovery timeout.
is_ready = payload.get("status") == "ready" or payload.get("ready") is True
raise SystemExit(0 if is_ready else 1)
' 2>/dev/null); then
        READY_OK=1
        break
    fi
    sleep 5
done
if [ "$READY_OK" != 1 ]; then
    echo "RUNTIME NOT READY — prerequisite recovery or lane health did not converge" >&2
    docker compose exec -T multi-lane-shadow python -c '
import urllib.error
import urllib.request

try:
    print(urllib.request.urlopen("http://127.0.0.1:8080/ready", timeout=5).read().decode())
except urllib.error.HTTPError as exc:
    print(exc.read().decode())
' >&2 || true
    docker compose logs --tail=80 multi-lane-shadow >&2 || true
    exit 1
fi
echo "runtime readiness OK: $READY_PAYLOAD"
exit 0
}
