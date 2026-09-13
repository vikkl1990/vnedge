#!/usr/bin/env python3
"""Host-only, bounded recovery of already-running unhealthy Compose services.

No Docker socket in the app, no venue access, no readiness/strategy gates.
Deliberately stopped/missing services require operator action, not resurrection.
Run from the repository with the same OS user as scripts/deploy.sh.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SERVICES = frozenset({
    "multi-lane-shadow", "dashboard-tls", "pulse-recorder", "delta-recorder",
    "tick-lake-maintenance", "research-loop", "agent-job-runner",
    "agentic-research-os", "canonical-parity-evidence", "quote-parity-evidence",
    "execution-path-audit", "ml-pipeline-status", "scanner-evidence",
})
MIN_UPTIME = 600
COOLDOWN = 900
MAX_RESTARTS_PER_HOUR = 2


def assess(service: str, container: dict[str, Any], previous: dict[str, Any], now: float) -> dict[str, Any]:
    """Pure policy; no recovery from market denies or stale research results."""
    state = container.get("State", {})
    health = state.get("Health", {}).get("Status", "unprobed")
    history = [t for t in previous.get("restarts", []) if 0 <= now - t < 3600]
    failures = int(previous.get("failures", 0)) + 1 if health == "unhealthy" else 0
    result = {"service": service, "running": state.get("Running") is True,
              "health": health, "failures": failures, "restarts": history, "action": "none"}
    if service not in SERVICES:
        result["action"] = "excluded"
    elif state.get("Running") is not True:
        result["action"] = "operator_required_stopped_or_missing"
    elif health == "unhealthy":
        try:
            uptime = now - datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError):
            uptime = -1
        if uptime < MIN_UPTIME or failures < 3:
            result["action"] = "grace"
        elif len(history) >= MAX_RESTARTS_PER_HOUR:
            result["action"] = "operator_required_restart_budget"
        elif history and now - max(history) < COOLDOWN:
            result["action"] = "cooldown"
        else:
            result["action"] = "restart"
    return result


def command(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=90).stdout


def save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="enable bounded restart; default read-only")
    parser.add_argument("--state", type=Path, default=Path("data/reports/service_watchdog.json"))
    args = parser.parse_args()
    # Serialize both probes and recovery with sanctioned deployments. Never
    # race a recreate or restart a service while a deployment restores it.
    with open("/tmp/vnedge-deploy.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"action":"deployment_in_progress"}')
            return 0
        try:
            previous = json.loads(args.state.read_text()) if args.state.exists() else {}
            ids = command("docker", "compose", "ps", "--all", "--quiet").split()
            containers = json.loads(command("docker", "inspect", *ids)) if ids else []
            seen: dict[str, dict[str, Any]] = {}
            for container in containers:
                labels = container.get("Config", {}).get("Labels", {})
                service = labels.get("com.docker.compose.service", "")
                if service not in SERVICES:
                    continue
                if service in seen:
                    raise ValueError("duplicate service instances; operator review required")
                seen[service] = container
            rows = {}
            recovered = False
            now = time.time()
            # Only services that exist are governed. Profiles remain opt-in.
            for service, container in sorted(seen.items()):
                row = assess(service, container, previous.get("services", {}).get(service, {}), now)
                rows[service] = row
                if row["action"] == "restart":
                    if not args.apply:
                        row["action"] = "would_restart"
                    elif recovered:
                        row["action"] = "deferred_one_per_cycle"
                    else:
                        # Write the attempt BEFORE side effect, so interruption
                        # or a failed Docker call cannot reset the budget.
                        row["restarts"].append(now)
                        save(args.state, {"generated_at": now, "services": {**previous.get("services", {}), **rows}})
                        command("docker", "restart", "--time", "30", container["Id"])
                        row["action"] = "restarted_pending_health"
                        recovered = True
            # Core services absent entirely must be visible, never autostarted.
            for service in ("multi-lane-shadow", "dashboard-tls", "pulse-recorder", "delta-recorder"):
                if service not in rows:
                    rows[service] = assess(service, {}, {}, now)
            payload = {"generated_at": now, "apply": args.apply, "services": rows,
                       "can_trade": False, "can_promote": False}
            if args.apply:
                save(args.state, payload)
            print(json.dumps(payload, sort_keys=True))
            return 1 if any(r["action"].startswith("operator_required") for r in rows.values()) else 0
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # Do not print command output/environment: keep credentials out.
            print(json.dumps({"action": "watchdog_failed", "error_type": type(exc).__name__}))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
