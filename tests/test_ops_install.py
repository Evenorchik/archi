"""`archi service install` on a running door: the restart follows `archi restart`'s policy (wait for idle;
from inside a turn it is handed to a transient unit), even when the unit file changed. systemctl is faked."""
import subprocess

from ops import service


def fake_systemd(monkeypatch, tmp_path, *, idle: bool):
    calls = []
    monkeypatch.setattr(service, "have_systemd", lambda: True)
    monkeypatch.setattr(service, "systemd_usable", lambda: True)
    monkeypatch.setattr(service, "UNIT_DIR", tmp_path / "units")
    (tmp_path / "units").mkdir(exist_ok=True)
    monkeypatch.setattr(service, "systemctl", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(service, "unit_state", lambda door: {"ActiveState": "active"})
    monkeypatch.setattr(service, "wait_active", lambda door, timeout=15: True)
    monkeypatch.setattr(service, "wait_idle", lambda home=None, **kw: idle)
    deferred = []
    monkeypatch.setattr(service, "detach_restart", lambda **kw: deferred.append(kw) or 0)
    return calls, deferred


def restarts(calls):
    return [c for c in calls if c and c[0] == "restart"]


def test_a_changed_unit_of_a_busy_door_is_not_restarted_now(tmp_path, monkeypatch, capsys):
    calls, deferred = fake_systemd(monkeypatch, tmp_path, idle=False)
    (tmp_path / "units" / "archi-telegram.service").write_text("an older unit\n")
    assert service.install_unit("telegram", home=tmp_path) == 0
    assert ("daemon-reload",) in calls and restarts(calls) == [] and deferred == []
    assert "turns are busy, so it was not restarted and still runs the old unit" in capsys.readouterr().out


def test_an_idle_door_is_restarted(tmp_path, monkeypatch):
    calls, _ = fake_systemd(monkeypatch, tmp_path, idle=True)
    (tmp_path / "units" / "archi-telegram.service").write_text("an older unit\n")
    assert service.install_unit("telegram", home=tmp_path) == 0
    assert restarts(calls) == [("restart", "archi-telegram.service")]


def test_inside_a_turn_the_restart_is_handed_over(tmp_path, monkeypatch):
    calls, deferred = fake_systemd(monkeypatch, tmp_path, idle=True)
    monkeypatch.setenv("ARCHI_CONVERSATION", "tg:1001:77")
    (tmp_path / "units" / "archi-telegram.service").write_text("an older unit\n")
    assert service.install_unit("telegram", home=tmp_path) == 0
    assert restarts(calls) == [] and len(deferred) == 1 and deferred[0]["delay"] >= service.IN_TURN_MIN_DELAY_S
