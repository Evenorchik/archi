"""Starting work that must outlive the process that asks for it, and taking such work down again.

Two kinds of work in Archi must survive both the turn that starts them (a turn's CLI process tree is killed
when the turn ends) and a restart of the door (systemd kills the door unit's whole cgroup): learning reviews
and durable jobs. `spawn()` starts them

  * with systemd: as a transient service unit (`systemd-run --unit <name> --collect`), outside the caller's
    cgroup. systemd reaps it, and `systemctl stop <unit>` takes its whole process tree down;
  * otherwise, or when systemd-run fails: through a short-lived launcher that starts the process in a new
    session and exits at once, so the process is re-parented to init (the classic double fork, done with
    subprocess because os.fork() is unsafe in a threaded parent such as the door).

ARCHI_DETACH = auto (default) | systemd | setsid | off. `off` starts nothing: the test suite sets it, so no
test ever leaves a unit or a stray review behind. Transient unit names start with ARCHI_UNIT_PREFIX (default
"archi").

Limit of the fallback: a double fork leaves the caller's process tree, not its cgroup. On a machine without
systemd that is all there is; but when systemd runs the door and only systemd-run failed, work started this
way still sits in the door's cgroup and a door restart kills it. The result carries a warning then.

The process gets exactly the environment it is given. systemd-run receives that environment on its command
line, so it must never hold secrets: Archi's processes read their credentials from config/archi.env.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .running import pid_alive

MODES = ("auto", "systemd", "setsid", "off")


def mode() -> str:
    m = os.environ.get("ARCHI_DETACH", "auto").strip().lower() or "auto"
    return m if m in MODES else "auto"


def systemd_usable() -> bool:
    """systemd runs this machine, systemd-run exists and we may start system units (root)."""
    return (Path("/run/systemd/system").is_dir() and shutil.which("systemd-run") is not None
            and os.geteuid() == 0)


def unit_name(kind: str, ident: str) -> str:
    """Transient unit name: <prefix>-<kind>-<ident>, e.g. archi-job-20261125-140512-ab12cd."""
    prefix = re.sub(r"[^A-Za-z0-9_-]", "", os.environ.get("ARCHI_UNIT_PREFIX", "")) or "archi"
    return re.sub(r"[^A-Za-z0-9_.-]", "-", f"{prefix}-{kind}-{ident}")


@dataclass
class Spawned:
    ok: bool
    via: str = ""      # "systemd-run" | "setsid" | "off"
    unit: str = ""     # the transient unit (systemd-run), e.g. archi-job-<id>.service
    pid: int = 0       # the detached process (setsid)
    error: str = ""
    warning: str = ""  # started, but by the fallback although systemd runs (see the module docstring)


# Runs as `python -c` with the spec on stdin (never on the command line: the environment is in it).
_LAUNCHER = r"""
import json, os, subprocess, sys
spec = json.loads(sys.stdin.read())
if spec.get("nice"):
    try:
        os.nice(int(spec["nice"]))
    except OSError:
        pass
out = open(spec["log"], "ab") if spec.get("log") else subprocess.DEVNULL
p = subprocess.Popen(spec["argv"], cwd=spec.get("cwd") or None, env=spec["env"], stdin=subprocess.DEVNULL,
                     stdout=out, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
sys.stdout.write(str(p.pid))
sys.stdout.flush()
os._exit(0)
"""


def systemd_run_command(argv: list[str], *, name: str, description: str = "", env: dict | None = None,
                        cwd: str | os.PathLike | None = None, log_path: str | os.PathLike | None = None,
                        nice: int | None = None) -> list[str]:
    cmd = ["systemd-run", "--unit", name, "--collect", "--quiet"]
    if description:
        cmd += ["--description", description]
    if nice is not None:
        cmd += ["--property", f"Nice={int(nice)}"]
    if cwd:
        cmd += ["--working-directory", str(cwd)]
    if log_path:
        cmd += ["--property", f"StandardOutput=append:{log_path}", "--property", f"StandardError=append:{log_path}"]
    for k, v in sorted((env or {}).items()):
        cmd += ["--setenv", f"{k}={v}"]
    return cmd + [str(a) for a in argv]


def spawn(argv: list[str], *, name: str, description: str = "", env: dict | None = None,
          cwd: str | os.PathLike | None = None, log_path: str | os.PathLike | None = None,
          nice: int | None = None, timeout: float = 15) -> Spawned:
    """Start `argv` detached (see the module docstring). Returns quickly; never raises."""
    m = mode()
    if m == "off":
        return Spawned(False, "off", error="detached processes are turned off here (ARCHI_DETACH=off)")
    env = {str(k): str(v) for k, v in (os.environ if env is None else env).items()}
    try:
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return Spawned(False, m, error=f"log directory: {e}")
    first_error = ""
    if m == "systemd" or (m == "auto" and systemd_usable()):
        cmd = systemd_run_command(argv, name=name, description=description, env=env, cwd=cwd, log_path=log_path,
                                  nice=nice)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if res.returncode == 0:
                return Spawned(True, "systemd-run", unit=f"{name}.service")
            first_error = f"systemd-run failed: {(res.stderr or res.stdout).strip()[-300:]}"
        except (OSError, subprocess.TimeoutExpired) as e:
            first_error = f"systemd-run failed: {e}"
        if m == "systemd":
            return Spawned(False, "systemd-run", error=first_error)
    res = _setsid(argv, env=env, cwd=cwd, log_path=log_path, nice=nice, timeout=timeout)
    if not res.ok and first_error:
        res.error = f"{first_error}; {res.error}"
    elif res.ok and first_error:
        res.warning = (f"{first_error}; started as a detached process instead, which a restart of the door's "
                       "service can still stop")
    return res


def _setsid(argv, *, env, cwd, log_path, nice, timeout) -> Spawned:
    spec = {"argv": [str(a) for a in argv], "env": env, "cwd": str(cwd) if cwd else None,
            "log": str(log_path) if log_path else None, "nice": nice}
    try:
        p = subprocess.run([sys.executable, "-c", _LAUNCHER], input=json.dumps(spec), capture_output=True,
                           text=True, timeout=timeout, env={"PATH": env.get("PATH") or os.defpath},
                           start_new_session=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Spawned(False, "setsid", error=f"launcher failed: {e}")
    out = p.stdout.strip()
    if p.returncode != 0 or not out.isdigit():
        tail = (p.stderr or p.stdout).strip().splitlines()
        return Spawned(False, "setsid", error=f"launcher failed: {tail[-1] if tail else f'exit code {p.returncode}'}")
    return Spawned(True, "setsid", pid=int(out))


# ---------------------------------------------------------------------------------------------- processes

def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the command name (field 3, the state, is index 0)."""
    try:
        data = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return None
    tail = data.rsplit(")", 1)
    return tail[1].split() if len(tail) == 2 else None


def start_ticks(pid: int) -> int | None:
    """When the process started (clock ticks since boot): with the pid it identifies one process even after
    the pid number has been reused."""
    f = _stat_fields(pid)
    try:
        return int(f[19]) if f else None
    except (IndexError, ValueError):
        return None


def same_process(pid, ticks: int | None = None, boot: str | None = None) -> bool:
    """True while the process recorded as (pid, start ticks, boot id) is still running."""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not pid_alive(pid):
        return False
    if boot and boot_id() and boot != boot_id():
        return False   # recorded before a reboot: that process is gone whatever runs under its pid now
    if ticks is not None:
        now = start_ticks(pid)
        if now is not None and now != ticks:
            return False
    return True


def _parents() -> dict[int, int]:
    out = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for d in names:
        if d.isdigit():
            f = _stat_fields(int(d))
            if f and len(f) > 1 and f[1].lstrip("-").isdigit():
                out[int(d)] = int(f[1])
    return out


def descendants(pid: int) -> list[int]:
    """Every process below `pid`, through /proc (children that started their own session included)."""
    children: dict[int, list[int]] = {}
    for child, parent in _parents().items():
        children.setdefault(parent, []).append(child)
    out: list[int] = []
    todo = [int(pid)]
    while todo:
        for c in children.get(todo.pop(), []):
            if c not in out:
                out.append(c)
                todo.append(c)
    return out


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def kill_tree(pid: int, *, grace: float = 5.0) -> list[int]:
    """SIGTERM `pid` and all its descendants, SIGKILL whatever is still alive after `grace` seconds.
    The tree is collected before the first signal (a dead parent's children are re-parented and could not
    be found any more). Never signals this process. Returns the pids it signalled."""
    me = os.getpid()
    if int(pid) in (me, os.getppid(), 0, 1):
        return []          # never this process, its parent or init (a stale pid record must not do that)
    # Every target is remembered with its start time: a pid freed and reused during the grace period belongs
    # to another process and is left alone.
    targets = [(p, start_ticks(p)) for p in [int(pid), *descendants(pid)] if p != me]

    def alive(p: int, ticks: int | None) -> bool:
        return pid_alive(p) and (ticks is None or start_ticks(p) == ticks)

    for p, _t in targets:
        _signal(p, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(alive(p, t) for p, t in targets):
        time.sleep(0.05)
    known = {p for p, _t in targets}
    late = [(d, start_ticks(d)) for p, t in targets if alive(p, t) for d in descendants(p)
            if d not in known and d != me]
    for p, t in targets + late:
        if p != me and alive(p, t):
            _signal(p, signal.SIGKILL)
    return [p for p, _t in targets + late]
