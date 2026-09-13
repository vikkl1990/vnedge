from datetime import UTC, datetime
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("service_watchdog", Path(__file__).parents[1] / "scripts/service_watchdog.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NOW = 1_789_300_000.0


def container(health="unhealthy", running=True, uptime=3600):
    return {"State": {"Running": running, "Health": {"Status": health},
                      "StartedAt": datetime.fromtimestamp(NOW - uptime, UTC).isoformat()}}


def test_only_persistent_process_unhealth_recovers():
    assess = module.assess
    assert assess("dashboard-tls", container(), {"failures": 2}, NOW)["action"] == "restart"
    for health in ("healthy", "starting", "unprobed"):
        assert assess("dashboard-tls", container(health), {}, NOW)["action"] == "none"
    assert assess("dashboard-tls", container(), {}, NOW)["action"] == "grace"
    assert assess("dashboard-tls", container(uptime=50), {"failures": 10}, NOW)["action"] == "grace"


def test_never_resurrect_legacy_or_operator_stopped_services():
    for name in ("gap-recovery", "vision-recovery", "book-recorder", "live-trader"):
        assert module.assess(name, container(), {"failures": 99}, NOW)["action"] == "excluded"
    assert module.assess("delta-recorder", container(running=False), {}, NOW)["action"].startswith("operator_required")
    assert module.assess("delta-recorder", {}, {}, NOW)["action"].startswith("operator_required")


def test_budget_survives_healthy_interlude_and_cooldown():
    state = {"failures": 99, "restarts": [NOW - 100]}
    assert module.assess("delta-recorder", container(), state, NOW)["action"] == "cooldown"
    healthy = module.assess("delta-recorder", container("healthy"), state, NOW)
    assert healthy["restarts"] == state["restarts"]
    state["restarts"] = [NOW - 2500, NOW - 1000]
    assert module.assess("delta-recorder", container(), state, NOW)["action"] == "operator_required_restart_budget"


def test_invalid_uptime_never_restarts():
    value = container()
    value["State"]["StartedAt"] = "bad"
    assert module.assess("dashboard-tls", value, {"failures": 99}, NOW)["action"] == "grace"


def test_restart_is_journaled_first_and_limited_to_one_service(tmp_path, monkeypatch):
    import json
    import sys
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"services": {
        name: {"failures": 2} for name in ("dashboard-tls", "delta-recorder")
    }}))
    containers = []
    for name in ("dashboard-tls", "delta-recorder", "multi-lane-shadow", "pulse-recorder"):
        c = container("unhealthy" if name in ("dashboard-tls", "delta-recorder") else "healthy")
        c.update(Id=name, Config={"Labels": {"com.docker.compose.service": name}})
        containers.append(c)
    calls = []
    def command(*args):
        if args[1] == "compose":
            return "ids"
        if args[1] == "inspect":
            return json.dumps(containers)
        calls.append(args)
        persisted = json.loads(state.read_text())
        assert persisted["services"][args[-1]]["restarts"] == [NOW]
        return "done"
    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    monkeypatch.setattr(sys, "argv", ["watchdog", "--apply", "--state", str(state)])
    assert module.main() == 0
    assert calls == [("docker", "restart", "--time", "30", "dashboard-tls")]
    assert json.loads(state.read_text())["services"]["delta-recorder"]["action"] == "deferred_one_per_cycle"
