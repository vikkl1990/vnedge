"""L8 audit fix: a programmatic kill persists to the KILL file and survives a
restart (like `touch KILL`), cleared only by removing the file + reset()."""
from vnedge.risk.kill_switch import KillSwitch


def test_programmatic_kill_persists_and_survives_restart(tmp_path):
    kf = tmp_path / "KILL"
    ks = KillSwitch(kill_file=kf)
    ks.activate("reconciliation mismatch")
    assert kf.exists()                       # persisted, not just in-memory
    assert KillSwitch(kill_file=kf).is_active is True   # a fresh process re-trips from the file
    kf.unlink()
    ks.reset("operator cleared")
    assert ks.is_active is False
