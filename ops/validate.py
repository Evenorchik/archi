"""`archi validate`: is the code and the config fit to (re)start? Run before every restart after a change.

Checks, in order: Python syntax of every module, imports in the venv (dependencies present, no import-time
errors), a dry start of each door (config, token format, construction; no network), config sanity. Problems
are FAIL (exit 1) or WARN (reported, exit 0).
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from core import engine
from core.config import CODE_DIR, PROVIDERS, Config

from .service import DOORS, venv_python

CODE_PACKAGES = ("core", "doors", "ops", "tools")
TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$")


class Report:
    def __init__(self, out=print):
        self.out = out
        self.failed = 0
        self.warned = 0

    def ok(self, what: str) -> None:
        self.out(f"  ok    {what}")

    def fail(self, what: str) -> None:
        self.failed += 1
        self.out(f"  FAIL  {what}")

    def warn(self, what: str) -> None:
        self.warned += 1
        self.out(f"  warn  {what}")


def python_files(code_dir: Path = CODE_DIR) -> list[Path]:
    files = [code_dir / "archi"]
    for pkg in CODE_PACKAGES:
        files += sorted(p for p in (code_dir / pkg).rglob("*.py") if "__pycache__" not in p.parts)
    return [f for f in files if f.is_file()]


def modules(code_dir: Path = CODE_DIR) -> list[str]:
    out = []
    for pkg in ("core", "doors", "ops"):
        for p in sorted((code_dir / pkg).rglob("*.py")):
            if "__pycache__" in p.parts or p.name == "__main__.py":
                continue
            rel = p.relative_to(code_dir).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts.pop()
            out.append(".".join(parts))
    return out


def check_syntax(rep: Report, code_dir: Path = CODE_DIR) -> None:
    files = python_files(code_dir)
    bad = 0
    for f in files:
        try:
            compile(f.read_text(encoding="utf-8"), str(f), "exec")
        except (SyntaxError, ValueError, UnicodeDecodeError) as e:
            bad += 1
            rep.fail(f"syntax: {f.relative_to(code_dir)}: {e}")
    if not bad:
        rep.ok(f"syntax: {len(files)} files")


def _run(cmd: list[str], code_dir: Path, home: Path, timeout: float = 60) -> tuple[int, str]:
    env = dict(os.environ, ARCHI_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run(cmd, cwd=code_dir, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


def check_imports(rep: Report, code_dir: Path, home: Path) -> None:
    py = venv_python(code_dir)
    mods = modules(code_dir)
    script = ("import importlib, sys, traceback\n"
              "bad = 0\n"
              "for m in sys.argv[1:]:\n"
              "    try:\n"
              "        importlib.import_module(m)\n"
              "    except BaseException as e:\n"
              "        bad += 1\n"
              "        print(f'{m}: {type(e).__name__}: {e}')\n"
              "sys.exit(1 if bad else 0)\n")
    rc, out = _run([py, "-c", script, *mods], code_dir, home)
    if rc == 0:
        rep.ok(f"imports: {len(mods)} modules ({py})")
    else:
        for line in out.splitlines()[-10:]:
            rep.fail(f"imports: {line}")


def check_doors(rep: Report, code_dir: Path, home: Path, cfg: Config) -> None:
    py = venv_python(code_dir)
    for door in DOORS:
        if door == "telegram" and not cfg.get("TELEGRAM_BOT_TOKEN"):
            continue   # reported by the config check
        rc, out = _run([py, "-m", f"doors.{door}", "--check"], code_dir, home)
        last = out.splitlines()[-1] if out else ""
        if rc == 0:
            rep.ok(f"dry start: {door} ({last})")
        else:
            rep.fail(f"dry start: {door}: {last or f'exit code {rc}'}")


def check_config(rep: Report, cfg: Config) -> None:
    before = rep.failed
    if not cfg.path.exists():
        rep.fail(f"config: {cfg.path} does not exist (run `archi setup`)")
        return
    mode = stat.S_IMODE(cfg.path.stat().st_mode)
    if mode & 0o077:
        rep.warn(f"config: {cfg.path} is mode {oct(mode)}, it holds secrets (chmod 600 it)")
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    if not token:
        rep.fail("config: TELEGRAM_BOT_TOKEN is not set (run `archi setup telegram`)")
    elif not TOKEN_RE.match(token):
        rep.fail("config: TELEGRAM_BOT_TOKEN does not look like a bot token (digits:secret)")
    provider = cfg.get("PROVIDER").strip().lower()
    if provider not in PROVIDERS:
        rep.fail(f"config: PROVIDER={provider!r}, expected one of {', '.join(PROVIDERS)}")
    else:
        binary = engine.resolve_binary(provider)
        if not (os.path.isabs(binary) and os.path.exists(binary)):
            rep.fail(f"config: the {provider} CLI is not installed (run `archi setup provider {provider}`)")
    effort = cfg.get("CLAUDE_EFFORT")
    if effort and effort not in engine.EFFORTS:
        rep.fail(f"config: CLAUDE_EFFORT={effort!r}, expected one of {', '.join(engine.EFFORTS)}")
    owner = cfg.get("OWNER_ID").strip()
    if owner and not re.fullmatch(r"-?\d+", owner):
        rep.fail(f"config: OWNER_ID={owner!r} is not a Telegram user id")
    if not owner and not cfg.get("PAIRING_CODE"):
        rep.warn("config: no owner and no pairing code: nobody can use the bot (run `archi pair`)")
    key = cfg.get("OPENAI_API_KEY")
    if key and not key.startswith("sk-"):
        rep.warn("config: OPENAI_API_KEY does not look like an OpenAI key")
    if rep.failed == before:
        rep.ok(f"config: {cfg.path}")


def check_layout(rep: Report, code_dir: Path) -> None:
    rulebook = code_dir / "CLAUDE.md"
    if not rulebook.exists():
        rep.fail("layout: CLAUDE.md is missing")
    agents = code_dir / "AGENTS.md"
    if not agents.exists():
        rep.warn("layout: AGENTS.md (-> CLAUDE.md, read by Codex) is missing")
    skills = code_dir / ".claude" / "skills"
    if not skills.exists():
        rep.warn("layout: .claude/skills (-> ../skills, skill discovery) is missing")


def run(*, code_dir: Path = CODE_DIR, cfg: Config | None = None, out=print) -> int:
    cfg = cfg or Config()
    rep = Report(out)
    out(f"Validating {code_dir} (home {cfg.home})")
    check_syntax(rep, code_dir)
    if rep.failed:
        out("FAILED: fix the syntax errors before anything else")
        return 1
    check_imports(rep, code_dir, cfg.home)
    before = rep.failed
    check_config(rep, cfg)
    if rep.failed == before:
        check_doors(rep, code_dir, cfg.home, cfg)
    check_layout(rep, code_dir)
    if rep.failed:
        out(f"FAILED: {rep.failed} problem(s); do not restart until they are fixed")
        return 1
    out("valid" + (f" ({rep.warned} warning(s))" if rep.warned else ""))
    return 0
