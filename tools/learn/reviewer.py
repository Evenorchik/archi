"""The reviewer: one sandboxed, non-interactive CLI run of the configured provider (claude or codex).

claude: `claude -p --output-format json --no-session-persistence --permission-mode acceptEdits
         --tools Read,Write,Edit,Glob,Grep --allowedTools Read,Glob,Grep --disallowedTools Bash,…
         --strict-mcp-config --add-dir <staged store> …` with cwd = the stage directory (no MCP servers of
        the host's configuration are started either). Only reading is pre-approved, anywhere;
        acceptEdits approves edits inside the working directories (the cwd and the --add-dir ones) and nowhere
        else, and in print mode nobody can approve anything more. No shell, web, sub-agents or skills. (The
        CLI also refuses writes under ~/.claude even when asked, which is one more reason the reviewer works
        on a staged copy of the memory store: the real one lives there.) The CLI's own auto-memory is
        switched off for the run (CLAUDE_CODE_DISABLE_AUTO_MEMORY=1), so the staged store is the only one.
codex:  `codex -a never exec -s workspace-write --skip-git-repo-check -C <stage> --json -`: its sandbox lets
        it write only inside the stage directory (and the system temp directory), without network.
The prompt goes in on stdin. The answer is the reviewer's short summary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import detach, engine  # noqa: E402

CLAUDE_TOOLS = "Read,Write,Edit,Glob,Grep"
CLAUDE_PREAPPROVED = "Read,Glob,Grep"
CLAUDE_DENIED = "Bash,WebFetch,WebSearch,Agent,Task,Workflow,Skill,NotebookEdit,TaskStop,Monitor"
# Turn context the reviewer must not inherit: it is not a turn of any conversation.
DROP_ENV = ("CLAUDECODE", "ARCHI_CONVERSATION", "ARCHI_CHAT_ID", "ARCHI_THREAD_ID", "ARCHI_DOOR",
            "ARCHI_TOPIC_TITLE")


@dataclass
class Result:
    ok: bool
    text: str = ""
    error: str = ""
    cost_usd: float | None = None
    seconds: float = 0.0
    command: list = field(default_factory=list)


def claude_command(write_dirs: list[Path], *, model: str = "", effort: str = "") -> list[str]:
    cmd = [engine.resolve_binary("claude"), "-p", "--output-format", "json", "--no-session-persistence",
           "--permission-mode", "acceptEdits", "--tools", CLAUDE_TOOLS, "--allowedTools", CLAUDE_PREAPPROVED,
           "--disallowedTools", CLAUDE_DENIED, "--strict-mcp-config"]
    if model:
        cmd += ["--model", model]
    if effort in engine.EFFORTS:
        cmd += ["--effort", effort]
    for d in write_dirs:
        cmd += ["--add-dir", str(d)]
    return cmd


def codex_command(workdir: Path, *, model: str = "") -> list[str]:
    cmd = [engine.resolve_binary("codex"), "-a", "never", "exec", "-s", "workspace-write",
           "--skip-git-repo-check", "-C", str(workdir), "--json"]
    if model:
        cmd += ["-m", model]
    return cmd + ["-"]


def _parse_claude(out: str) -> dict | None:
    out = out.strip()
    for chunk in (out, *reversed(out.splitlines())):
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def run(provider: str, prompt: str, *, workdir: Path, write_dirs: list[Path], model: str = "", effort: str = "",
        timeout_s: float = 900, cfg=None) -> Result:
    if provider not in engine.PROVIDERS:
        return Result(False, error=f"unknown provider {provider!r}")
    cmd = codex_command(workdir, model=model) if provider == "codex" else claude_command(
        write_dirs, model=model, effort=effort)
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env.update(engine.provider_env(provider, cfg))
    if provider == "claude":
        # Claude Code's own auto-memory would describe a second memory directory (the one of the stage's
        # path): the staged store must be the only memory the reviewer knows about.
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, cwd=str(workdir), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except FileNotFoundError:
        return Result(False, error=f"the {provider} CLI was not found ({cmd[0]})", command=cmd)
    except OSError as e:
        return Result(False, error=f"could not start {provider}: {e}", command=cmd)
    try:
        out_b, err_b = proc.communicate(prompt.encode(), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        detach.kill_tree(proc.pid, grace=3)
        engine.kill_tree(proc.pid)
        try:
            proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return Result(False, error=f"the reviewer ran longer than {int(timeout_s)} s and was stopped",
                      seconds=time.monotonic() - t0, command=cmd)
    out, err = out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")
    seconds = time.monotonic() - t0
    err_tail = (err.strip().splitlines() or [""])[-1][:300]
    if provider == "claude":
        data = _parse_claude(out)
        if data is None:
            return Result(False, error=f"claude exited with {proc.returncode}: {err_tail or out.strip()[-300:]}",
                          seconds=seconds, command=cmd)
        text = str(data.get("result") or "").strip()
        bad = bool(data.get("is_error")) or data.get("subtype") not in (None, "success")
        cost = data.get("total_cost_usd")
        return Result(not bad, text, (text or str(data.get("subtype") or "error"))[:500] if bad else "",
                      cost if isinstance(cost, (int, float)) else None, seconds, cmd)
    parser = engine.CodexStream()
    for line in out.splitlines():
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        if isinstance(evt, dict):
            parser.feed(evt)
    text = parser.final_text().strip()
    if parser.is_error or (not parser.result_seen and proc.returncode != 0):
        return Result(False, text, parser.error or f"codex exited with {proc.returncode}: {err_tail}", None,
                      seconds, cmd)
    return Result(True, text, "", None, seconds, cmd)
