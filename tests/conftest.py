import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FAKE = Path(__file__).resolve().parent / "fake_cli.py"


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """Point CLAUDE_BIN / CODEX_BIN at the fake CLI. Returns the log file it appends calls to."""
    log = tmp_path / "fake.log"
    for flavor in ("claude", "codex"):
        wrapper = tmp_path / f"fake-{flavor}"
        wrapper.write_text(f"#!/bin/sh\nFAKE_FLAVOR={flavor} exec {sys.executable} {FAKE} \"$@\"\n")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv(f"{flavor.upper()}_BIN", str(wrapper))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_PIDFILE", str(tmp_path / "pids"))
    monkeypatch.setenv("FAKE_MODE", "ok")
    return log


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("ARCHI_HOME", str(h))
    return h


@pytest.fixture(autouse=True)
def _no_detached_work(monkeypatch):
    """No test starts a learning review, a job or a systemd unit on the host unless it asks for it
    (core/detach.py: ARCHI_DETACH=off starts nothing)."""
    monkeypatch.setenv("ARCHI_DETACH", "off")
    monkeypatch.delenv("ARCHI_CONVERSATION", raising=False)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/status").read_text()
        return "State:\tZ" not in state
    except OSError:
        return False
