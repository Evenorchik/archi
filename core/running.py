"""Markers of running turns: one small JSON file per turn in progress, in state/running/.

A door writes the marker when a turn starts and removes it after the answer was delivered. Other processes
read the markers: `archi restart --when-idle` waits until there are none (a restart kills every running turn),
`archi status` shows them, and a door that starts finds the turns its predecessor was running when it died
and tells those conversations that their task was interrupted.

A marker counts as running only while the process that wrote it is alive and its deadline (start + the
hard turn limit) has not passed by more than a margin; anything else is a leftover. Leftovers are removed
only by a door of the same kind at startup, after it has reported them, so a status check in between never
swallows a restart notice.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .config import atomic_write, home

STALE_MARGIN_S = 600          # a turn past its deadline by this much is certainly dead
DEFAULT_HARD_S = 8 * 3600


def run_dir(home_dir: Path | str | None = None) -> Path:
    return Path(home_dir or home()) / "state" / "running"


def _file_name(door: str, key: str) -> str:
    return f"{re.sub(r'[^A-Za-z0-9_-]', '_', door)}--{re.sub(r'[^A-Za-z0-9_.-]', '_', key)}.json"


def start(door: str, key: str, home_dir: Path | str | None = None, *, hard_s: float = DEFAULT_HARD_S,
          pid: int | None = None, now: float | None = None) -> Path:
    """Record that a turn of conversation `key` is running in this process. Returns the marker path."""
    now = time.time() if now is None else now
    path = run_dir(home_dir) / _file_name(door, key)
    data = {"door": door, "key": key, "pid": pid or os.getpid(), "started_at": now, "deadline": now + hard_s}
    atomic_write(path, json.dumps(data) + "\n", mode=0o644)
    return path


def finish(path: Path | str | None) -> None:
    if path:
        Path(path).unlink(missing_ok=True)


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    try:
        return "State:\tZ" not in Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return True


def _read(home_dir) -> list[dict]:
    d = run_dir(home_dir)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = {"door": "", "key": p.stem, "pid": 0, "started_at": 0, "deadline": 0}
        data["path"] = str(p)
        out.append(data)
    return out


def _fresh(m: dict, now: float) -> bool:
    deadline = float(m.get("deadline") or (float(m.get("started_at") or 0) + DEFAULT_HARD_S))
    return pid_alive(m.get("pid")) and now <= deadline + STALE_MARGIN_S


def active(home_dir: Path | str | None = None, *, now: float | None = None) -> list[dict]:
    """Turns running right now (any door). Each item: door, key, pid, started_at, age (seconds)."""
    now = time.time() if now is None else now
    out = []
    for m in _read(home_dir):
        if _fresh(m, now):
            m["age"] = max(0.0, now - float(m.get("started_at") or now))
            out.append(m)
    return out


def leftovers(door: str, home_dir: Path | str | None = None, *, now: float | None = None) -> list[dict]:
    """Markers of `door` left by a process that is gone (it was killed or restarted mid-turn). They are
    removed here; the caller reports them. Markers of another live process are never touched."""
    now = time.time() if now is None else now
    out = []
    for m in _read(home_dir):
        if m.get("door") not in (door, ""):
            continue
        if int(m.get("pid") or 0) == os.getpid() or _fresh(m, now):
            continue
        finish(m["path"])
        out.append(m)
    return out
