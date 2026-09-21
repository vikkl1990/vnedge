#!/usr/bin/env bash
# Local SSH wrapper. All recreation goes through the VM's serialized deploy.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
mode=${1:---check}
case "$mode" in --check|--deploy|--cleanup-build-cache) ;; *) echo 'Use --check, --deploy, or --cleanup-build-cache' >&2; exit 2;; esac
: "${VNEDGE_VM_SSH_TARGET:?Set VNEDGE_VM_SSH_TARGET=user@host}"
[[ "$VNEDGE_VM_SSH_TARGET" =~ ^[a-zA-Z0-9_.@-]+$ ]] || exit 2
ssh_args=(-o BatchMode=yes -o ConnectTimeout=20)
if [ -n "${VNEDGE_VM_SSH_KEY:-}" ]; then ssh_args+=(-i "$VNEDGE_VM_SSH_KEY"); fi
sha=$(git rev-parse HEAD)
if [ "$mode" = --deploy ] && [ -n "$(git status --porcelain)" ]; then
    echo 'Commit local changes before deployment.' >&2; exit 1
fi
ssh "${ssh_args[@]}" "$VNEDGE_VM_SSH_TARGET" bash -s -- "$mode" "$sha" <<'REMOTE'
set -euo pipefail
mode=$1
expected=$2
# Deliberately fixed, documented destination: never infer another checkout.
cd /home/ubuntu/vnedge
case "$mode" in
--check)
    git status --short
    git rev-parse HEAD
    docker compose ps
    df -h /
    docker system df
    ;;
--deploy)
    [ -z "$(git status --porcelain)" ] || { echo 'VM checkout dirty; refusing.' >&2; exit 1; }
    # Deployment lock is owned by scripts/deploy.sh. Snapshot metadata first;
    # no secrets, journals or market data are copied into this metadata file.
    backup=$(mktemp -d /home/ubuntu/vnedge-deploy-backup.XXXXXXXX)
    git rev-parse HEAD > "$backup/previous-sha.txt"
    docker compose ps --format json > "$backup/services.json"
    docker compose config --services > "$backup/service-names.txt"
    while read -r cid; do
        [ -n "$cid" ] || continue
        img=$(docker inspect --format '{{.Image}}' "$cid")
        svc=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$cid")
        docker tag "$img" "vnedge-rollback:${svc}-$(basename "$backup")"
    done < <(docker compose ps -q)
    echo "Rollback metadata and image tags: $backup"
    # Bootstrap ONLY the deploy script from the requested, pushed main commit;
    # execute its immutable copy so it performs the normal fetch/merge itself.
    git fetch origin main
    [ "$(git rev-parse origin/main)" = "$expected" ] || { echo 'Push local HEAD to origin/main first.' >&2; exit 1; }
    # Existing deploy.sh changes cwd based on its own location, so run through
    # bash with its code on stdin from the repository's scripts directory.
    git show "$expected:scripts/deploy.sh" > "$backup/deploy.sh"
    cd scripts
    VNEDGE_EXPECTED_SHA="$expected" COMPOSE_PROFILES=research,analyst \
        bash -c "$(< "$backup/deploy.sh")" ./deploy.sh
    ;;
--cleanup-build-cache)
    exec 9>/tmp/vnedge-deploy.lock
    flock -n 9 || { echo 'Deployment active; cleanup refused.' >&2; exit 1; }
    docker system df
    # Only regenerable build cache older than seven days. Never system/volume
    # prune; rollback image tags and all containers remain untouched.
    docker builder prune --force --filter until=168h
    docker system df
    ;;
esac
REMOTE
