"""The engine: one agent turn through a coding-agent CLI, for both providers.

    result = await run_turn("claude", prompt, session_id, cwd=home, on_event=cb)

* claude: `claude -p --output-format stream-json --verbose --include-partial-messages --model … [--effort …]
  --allowedTools … --append-system-prompt … [--resume <session>]`, prompt on stdin (plain text, or a
  stream-json user message when images are attached).
* codex: `codex -a <approval> exec -s <sandbox> [resume] --json [-m model] [-i image …] [<session>] -`, prompt
  (with the system prompt in front, codex has no flag for it) on stdin.

Both streams are parsed into the same events, passed to `on_event(Event)`:
  step  a tool call the owner can see, `text` = short human label;
  text  the assistant text so far (replace, do not append: it is the current preview);
  bg    a background command started (`data["state"] == "running"`) or finished;
  done  the turn is over, `result` = the TurnResult.

Deadlines: a silence limit (no output at all) and a hard ceiling. On a deadline, on cancellation (the
`cancel` event, or the awaiting task being cancelled) the whole process group is killed: the CLI spawns
tool subprocesses and sub-agents, and killing one pid would orphan them.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

PROVIDERS = ("claude", "codex")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_ALLOWED_TOOLS = "Read,Write,Edit,MultiEdit,Bash,Glob,Grep,WebFetch,WebSearch,Task,Agent,Workflow,Skill,TodoWrite"
_IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
                "webp": "image/webp"}
_STALE_SESSION = re.compile(
    r"(?i)no conversation found|no (saved )?(session|rollout|thread)[^\n]{0,40}found|"
    r"(session|thread|rollout)[^\n]{0,40}not found")
_BG_ID = re.compile(r"running in background with ID: (\w+)")
_BG_DONE = re.compile(r"<task-id>(\w+)</task-id>.*?<status>(\w+)</status>", re.S)


@dataclass
class Event:
    kind: str                 # "step" | "text" | "bg" | "done"
    text: str = ""
    tool: str = ""
    data: dict = field(default_factory=dict)
    result: "TurnResult | None" = None


@dataclass
class TurnResult:
    answer: str
    session_id: str | None
    ok: bool
    error: str = ""
    cancelled: bool = False
    timed_out: bool = False
    steps: int = 0


@dataclass
class Limits:
    silence_s: float = float(os.environ.get("ARCHI_SILENCE_LIMIT", 3600))   # no output at all for this long
    hard_s: float = float(os.environ.get("ARCHI_HARD_LIMIT", 8 * 3600))     # ceiling even while streaming
    kill_grace_s: float = 3.0                                               # SIGTERM -> SIGKILL


# ---------------------------------------------------------------------------------------------- helpers

def resolve_binary(name: str) -> str:
    """Path of a provider CLI. Honours CLAUDE_BIN / CODEX_BIN, then PATH, then the usual user-level
    install locations (a systemd service often runs with a PATH that does not include them)."""
    env_name = f"{name.upper()}_BIN"
    if os.environ.get(env_name):
        return os.environ[env_name]
    found = shutil.which(name)
    if found:
        return found
    for d in ("~/.local/bin", "~/.npm-global/bin", "/usr/local/bin", "~/.bun/bin"):
        p = Path(d).expanduser() / name
        if p.exists():
            return str(p)
    return name


def neutralize_leading_slash(prompt: str) -> str:
    """A prompt starting with "/" is parsed by the CLI as a slash command: the model is never called, and
    commands the CLI knows (/clear, /init, every installed skill) would be executed. Wrap it as text."""
    if not prompt.lstrip().startswith("/"):
        return prompt
    return "[user message, verbatim: the leading slash is part of the text, not a command]\n" + prompt


def _cut(s: str, n: int = 90) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _base(p: str | None) -> str:
    return os.path.basename(str(p or "").rstrip("/")) or "file"


def claude_step_label(name: str, inp: dict) -> str | None:
    """Short human label for one Claude Code tool call, or None for plumbing nobody needs to see."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return _cut(inp.get("description") or inp.get("command") or "Running a command")
    if name == "Write":
        return f"Writing {_base(inp.get('file_path'))}"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Editing {_base(inp.get('file_path') or inp.get('notebook_path'))}"
    if name == "Read":
        return f"Reading {_base(inp.get('file_path'))}"
    if name == "Grep":
        return _cut(f"Searching for “{inp.get('pattern', '')}”")
    if name == "Glob":
        return _cut(f"Looking for files {inp.get('pattern', '')}")
    if name == "WebSearch":
        return _cut(f"Web search: {inp.get('query', '')}")
    if name == "WebFetch":
        return f"Opening {urlsplit(str(inp.get('url', ''))).hostname or 'a web page'}"
    if name in ("Agent", "Task"):
        return _cut(f"Sub-agent: {inp.get('description') or 'subtask'}")
    if name == "Workflow":
        return "Running a multi-agent workflow"
    if name == "Skill":
        return f"Using skill {inp.get('skill', '')}".strip()
    if name == "Monitor":
        return _cut(f"Watching: {inp.get('description', '')}")
    if name == "TaskStop":
        return "Stopping a background task"
    if name in ("ToolSearch", "ScheduleWakeup", "ReportFindings", "ListAgents", "TodoWrite"):
        return None
    if name.startswith("mcp__"):
        parts = name.split("__")
        return _cut(f"{parts[1]}: {parts[-1]}" if len(parts) >= 3 else name)
    return _cut(name)


def _unwrap_shell(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return command.strip()
    if len(parts) >= 3 and os.path.basename(parts[0]) in ("bash", "sh", "zsh") and parts[1] in ("-lc", "-c"):
        return parts[2].strip()
    return command.strip()


_QUIET_COMMANDS = ("cat ", "sed -n", "head ", "tail ", "rg ", "grep ", "find ", "ls", "pwd", "wc ", "stat ",
                   "git status", "git diff", "git show", "git log", "echo ")


def codex_command_label(command: str) -> str | None:
    """Short label for a shell command Codex runs; None for read-only micro-steps."""
    cmd = _unwrap_shell(command or "")
    if not cmd:
        return None
    low = cmd.lower()
    first = low.split("\n", 1)[0]
    if any(first.startswith(q) for q in _QUIET_COMMANDS):
        return None
    if "pytest" in low:
        return "Running tests"
    if "apply_patch" in low or "sed -i" in low or "perl -pi" in low or "perl -0pi" in low:
        return "Editing files"
    if re.search(r"\b(pip|npm|apt(-get)?|uv) (install|add)\b", low):
        return "Installing packages"
    if "systemctl restart" in low or " restart" in first:
        return "Restarting a service"
    m = re.search(r"https?://[^\s'\"]+", cmd)
    if m and ("curl" in low or "wget" in low):
        return f"Opening {urlsplit(m.group(0)).hostname or 'a web page'}"
    return _cut("Running: " + cmd.split("\n", 1)[0], 80)


def provider_env(provider: str, cfg=None) -> dict[str, str]:
    """Credentials a provider CLI gets from config/archi.env. Secrets live only in that file (the door's
    own environment does not carry them), so every CLI start reads them from there: a token written by
    `archi setup` works on the next turn without a restart.

    claude: CLAUDE_CODE_OAUTH_TOKEN (a `claude setup-token` token). When it is empty the CLI uses its own
    login (~/.claude/.credentials.json). codex keeps its login in ~/.codex/auth.json and needs nothing here."""
    if provider != "claude":
        return {}
    from .config import Config
    try:
        token = (cfg or Config()).get("CLAUDE_CODE_OAUTH_TOKEN").strip()
    except OSError:
        return {}
    return {"CLAUDE_CODE_OAUTH_TOKEN": token} if token else {}


# ---------------------------------------------------------------------------------------------- parsers

class ClaudeStream:
    """Parser for `claude -p --output-format stream-json --verbose [--include-partial-messages]`."""

    def __init__(self) -> None:
        self.answer = ""
        self.session_id: str | None = None
        self.last_text = ""
        self.partial = ""
        self.result_seen = False
        self.is_error = False
        self.error = ""
        self.steps = 0
        self._bg_pending: dict[str, str] = {}   # tool_use_id -> label until the shell id is known
        self._bg: dict[str, str] = {}           # background shell id -> label

    def feed(self, evt: dict) -> list[Event]:
        out: list[Event] = []
        t = evt.get("type")
        if t == "system":
            self.session_id = evt.get("session_id") or self.session_id
        elif t == "stream_event":
            e = evt.get("event") or {}
            if e.get("type") == "message_start":
                self.partial = ""
            elif e.get("type") == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    self.partial += d["text"]
                    out.append(Event("text", self.partial))
        elif t == "assistant":
            self.session_id = evt.get("session_id") or self.session_id
            for part in (evt.get("message") or {}).get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    name = str(part.get("name") or "")
                    inp = part.get("input") or {}
                    label = claude_step_label(name, inp)
                    if label:
                        self.steps += 1
                        out.append(Event("step", label, tool=name))
                        if name == "Bash" and isinstance(inp, dict) and inp.get("run_in_background"):
                            self._bg_pending[str(part.get("id", ""))] = label
                elif part.get("type") == "text" and part.get("text"):
                    self.last_text = part["text"]
                    self.partial = ""
                    out.append(Event("text", self.last_text))
        elif t == "user":
            for part in (evt.get("message") or {}).get("content") or []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    text = json.dumps(part.get("content"), ensure_ascii=False) if not isinstance(
                        part.get("content"), str) else part["content"]
                    m = _BG_ID.search(text)
                    tid = str(part.get("tool_use_id", ""))
                    if m and tid in self._bg_pending:
                        label = self._bg_pending.pop(tid)
                        self._bg[m.group(1)] = label
                        out.append(Event("bg", label, data={"id": m.group(1), "state": "running"}))
                    out.extend(self._settle(text))
        elif t == "result":
            self.result_seen = True
            self.answer = evt.get("result") or ""
            self.session_id = evt.get("session_id") or self.session_id
            sub = evt.get("subtype")
            self.is_error = bool(evt.get("is_error")) or (sub not in (None, "success"))
            if self.is_error:
                self.error = (self.answer or str(sub or "error"))[:500]
        return out

    def _settle(self, text: str) -> list[Event]:
        out = []
        for tid, st in _BG_DONE.findall(text or ""):
            if tid in self._bg and st != "running":
                out.append(Event("bg", self._bg.pop(tid), data={"id": tid, "state": st}))
        return out

    def final_text(self) -> str:
        return self.answer or self.last_text


class CodexStream:
    """Parser for `codex exec --json` (thread/turn/item events)."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.last_text = ""
        self.answer = ""
        self.error = ""
        self.result_seen = False
        self.is_error = False
        self.steps = 0

    def feed(self, evt: dict) -> list[Event]:
        out: list[Event] = []
        t = evt.get("type", "")
        item = evt.get("item") or {}
        itype = item.get("type", "")
        if t == "thread.started":
            self.session_id = evt.get("thread_id") or self.session_id
        elif t == "item.started":
            label = None
            if itype == "command_execution":
                label = codex_command_label(str(item.get("command", "")))
            elif itype == "mcp_tool_call":
                label = _cut(f"{item.get('server', '')}: {item.get('tool', '')}".strip(": "))
            elif itype == "web_search":
                label = _cut(f"Web search: {item.get('query', '')}")
            if label:
                self.steps += 1
                out.append(Event("step", label, tool=itype))
        elif t == "item.completed":
            if itype == "agent_message" and (item.get("text") or "").strip():
                self.last_text = item["text"].strip()
                out.append(Event("text", self.last_text))
            elif itype == "file_change":
                names = [_base(c.get("path")) for c in item.get("changes") or [] if isinstance(c, dict)]
                if names:
                    self.steps += 1
                    out.append(Event("step", _cut("Editing " + ", ".join(names[:4])), tool=itype))
            elif itype == "web_search" and item.get("query"):
                pass
        elif t == "turn.completed":
            self.result_seen = True
            self.answer = self.last_text or self.answer
        elif t == "turn.failed":
            self.result_seen = True
            self.is_error = True
            self.error = str((evt.get("error") or {}).get("message") or "codex turn failed")[:500]
        elif t == "error":
            self.error = str(evt.get("message") or "codex error")[:500]
        return out

    def final_text(self) -> str:
        return self.answer or self.last_text


# ---------------------------------------------------------------------------------------------- commands

def _image_message(prompt: str, images: list[str]) -> bytes:
    """stream-json user message: base64 image blocks + text, the CLI's native image input."""
    content = []
    for p in images:
        try:
            data = base64.b64encode(Path(p).read_bytes()).decode()
        except OSError:
            continue
        ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
        content.append({"type": "image", "source": {"type": "base64",
                                                    "media_type": _IMAGE_TYPES.get(ext, "image/jpeg"),
                                                    "data": data}})
    if prompt:
        content.append({"type": "text", "text": prompt})
    return (json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n").encode()


def build_claude_command(prompt: str, session_id: str | None, *, model: str = "", effort: str = "",
                         system_prompt: str = "", images: list[str] | None = None,
                         partial: bool = True) -> tuple[list[str], bytes]:
    cmd = [resolve_binary("claude"), "-p", "--output-format", "stream-json", "--verbose"]
    if partial:
        cmd.append("--include-partial-messages")
    if model:
        cmd += ["--model", model]
    if effort in EFFORTS:
        cmd += ["--effort", effort]
    cmd += ["--allowedTools", os.environ.get("ARCHI_CLAUDE_TOOLS", CLAUDE_ALLOWED_TOOLS)]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    if session_id:
        cmd += ["--resume", session_id]
    if images:
        cmd += ["--input-format", "stream-json"]
        return cmd, _image_message(prompt, images)   # stream-json content is never slash-parsed
    # The prompt goes through stdin, not argv: no option parsing of a prompt that starts with "-",
    # and no E2BIG on a long paste.
    return cmd, neutralize_leading_slash(prompt).encode()


def build_codex_command(prompt: str, session_id: str | None, *, model: str = "", system_prompt: str = "",
                        images: list[str] | None = None) -> tuple[list[str], bytes]:
    approval = os.environ.get("ARCHI_CODEX_APPROVAL", "never")
    sandbox = os.environ.get("ARCHI_CODEX_SANDBOX", "danger-full-access")
    cmd = [resolve_binary("codex"), "-a", approval, "exec", "-s", sandbox]
    if session_id:
        cmd += ["resume"]
    cmd += ["--json"]
    if model:
        cmd += ["-m", model]
    for img in images or []:
        cmd += ["-i", img]
    if session_id:
        cmd += [session_id]
    cmd += ["-"]
    text = neutralize_leading_slash(prompt)
    if system_prompt:
        text = (f"[Context from the Archi door, not written by the user]\n{system_prompt}\n"
                f"[End of context. The user's message follows.]\n\n{text}")
    return cmd, text.encode()


# ---------------------------------------------------------------------------------------------- process

def kill_tree(pid: int, sig: int = signal.SIGKILL) -> None:
    """Signal the whole process group led by `pid` (the child is a group leader: start_new_session)."""
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


async def _terminate(proc: asyncio.subprocess.Process, grace: float) -> None:
    if proc.returncode is None:
        kill_tree(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            pass
    kill_tree(proc.pid, signal.SIGKILL)   # stragglers in the group, even if the leader is gone
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass   # reaped by init later; never block a conversation on a corpse


@dataclass
class _Outcome:
    rc: int | None
    stderr: str
    reason: str = ""          # "" | "cancelled" | "silence" | "hard"


async def _run_process(cmd: list[str], stdin: bytes | None, *, cwd: str, env: dict, parser,
                       emit: Callable[[Event], None], cancel: asyncio.Event | None,
                       limits: Limits) -> _Outcome:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,   # own process group, so the whole tree can be killed
    )
    err_tail: list[bytes] = []

    async def drain_stderr() -> None:
        # Drained concurrently: a child that fills the 64 KB stderr pipe would otherwise block forever.
        assert proc.stderr is not None
        try:
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    return
                err_tail.append(chunk)
                del err_tail[:-8]
        except Exception:
            return

    async def feed_stdin() -> None:
        if stdin is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    err_task = asyncio.create_task(drain_stderr())
    in_task = asyncio.create_task(feed_stdin())
    cancel_task = asyncio.create_task(cancel.wait()) if cancel is not None else None
    reason = ""
    started = time.monotonic()
    buf = b""

    def ingest(raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return
        try:
            evt = json.loads(line)
        except ValueError:
            return
        if not isinstance(evt, dict):
            return
        for ev in parser.feed(evt):
            emit(ev)

    try:
        assert proc.stdout is not None
        while True:
            remaining = limits.hard_s - (time.monotonic() - started)
            if remaining <= 0:
                reason = "hard"
                break
            read_task = asyncio.ensure_future(proc.stdout.read(65536))
            waiters = {read_task} | ({cancel_task} if cancel_task else set())
            done, _ = await asyncio.wait(waiters, timeout=min(limits.silence_s, remaining),
                                         return_when=asyncio.FIRST_COMPLETED)
            if read_task not in done:
                read_task.cancel()
                if cancel_task is not None and cancel_task in done:
                    reason = "cancelled"
                else:
                    reason = "silence" if limits.silence_s <= remaining else "hard"
                break
            chunk = read_task.result()
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:   # own line splitting: StreamReader.readline has a 64 KB line limit
                line, buf = buf.split(b"\n", 1)
                ingest(line)
        if buf.strip() and not reason:
            ingest(buf)
        if reason:
            await _terminate(proc, limits.kill_grace_s)
        else:
            try:
                await asyncio.wait_for(proc.wait(), timeout=30)
            except asyncio.TimeoutError:
                await _terminate(proc, limits.kill_grace_s)
    except BaseException:
        # Cancelled from outside (task.cancel()) or crashed: never leave the tree running.
        await asyncio.shield(_terminate(proc, limits.kill_grace_s))
        raise
    finally:
        if cancel_task is not None:
            cancel_task.cancel()
        in_task.cancel()
        try:
            await asyncio.wait_for(err_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            err_task.cancel()
    return _Outcome(proc.returncode, b"".join(err_tail).decode("utf-8", "replace"), reason)


# ---------------------------------------------------------------------------------------------- entry point

async def run_turn(provider: str, prompt: str, session_id: str | None, *, cwd: str | os.PathLike,
                   images: list[str] | None = None, on_event: Callable[[Event], None] | None = None,
                   env: dict | None = None, system_prompt: str = "", model: str = "", effort: str = "",
                   cancel: asyncio.Event | None = None, limits: Limits | None = None) -> TurnResult:
    """Run one turn and return TurnResult(answer, session_id, ok, error). Never raises for provider
    failures (they come back as ok=False with `error`); re-raises CancelledError after killing the tree."""
    limits = limits or Limits()

    def emit(ev: Event) -> None:
        if on_event is None:
            return
        try:
            on_event(ev)
        except Exception:
            pass   # a broken status display must never break the turn

    if provider not in PROVIDERS:
        res = TurnResult("", session_id, False, f"unknown provider {provider!r}")
        emit(Event("done", result=res))
        return res

    child_env = dict(os.environ)
    child_env.pop("CLAUDECODE", None)   # a nested CLI must not think it runs inside another session
    child_env.update(provider_env(provider))
    child_env.update({k: str(v) for k, v in (env or {}).items()})

    res = await _attempt(provider, prompt, session_id, cwd=str(cwd), images=images, env=child_env,
                         system_prompt=system_prompt, model=model, effort=effort, cancel=cancel,
                         limits=limits, emit=emit)
    if (session_id and not res.ok and not res.cancelled and not res.timed_out
            and _STALE_SESSION.search(res.error or "")):
        # The stored session points at a transcript that no longer exists: every later message would
        # fail the same way. Start fresh once; the caller stores the new session id.
        res = await _attempt(provider, prompt, None, cwd=str(cwd), images=images, env=child_env,
                             system_prompt=system_prompt, model=model, effort=effort, cancel=cancel,
                             limits=limits, emit=emit)
        if res.ok:
            res.error = "previous session was not found; started a new one"
    emit(Event("done", result=res))
    return res


async def _attempt(provider, prompt, session_id, *, cwd, images, env, system_prompt, model, effort,
                   cancel, limits, emit) -> TurnResult:
    if provider == "claude":
        cmd, stdin = build_claude_command(prompt, session_id, model=model, effort=effort,
                                          system_prompt=system_prompt, images=images)
        parser = ClaudeStream()
    else:
        cmd, stdin = build_codex_command(prompt, session_id, model=model, system_prompt=system_prompt,
                                         images=images)
        parser = CodexStream(session_id)
    try:
        out = await _run_process(cmd, stdin, cwd=cwd, env=env, parser=parser, emit=emit, cancel=cancel,
                                 limits=limits)
    except FileNotFoundError:
        return TurnResult("", session_id, False, f"{provider} CLI not found ({cmd[0]})")
    except OSError as e:
        return TurnResult("", session_id, False, f"could not start {provider}: {e}")

    text = parser.final_text().strip()
    sid = parser.session_id or (session_id if provider == "codex" else None)
    if out.reason == "cancelled":
        return TurnResult(text, sid, False, "cancelled", cancelled=True, steps=parser.steps)
    if out.reason:
        limit = limits.silence_s if out.reason == "silence" else limits.hard_s
        what = "no output for" if out.reason == "silence" else "the turn ran longer than"
        return TurnResult(text, sid, False, f"stopped: {what} {_human_s(limit)}", timed_out=True,
                          steps=parser.steps)
    err_tail = out.stderr.strip().splitlines()
    if parser.is_error:
        return TurnResult(text if not parser.answer else "", sid, False,
                          parser.error or (err_tail[-1] if err_tail else "error"), steps=parser.steps)
    if out.rc not in (0, None) and not parser.result_seen:
        detail = parser.error or (err_tail[-1] if err_tail else f"exit code {out.rc}")
        return TurnResult(text, sid, False, f"{provider} exited with {out.rc}: {detail}"[:600],
                          steps=parser.steps)
    if not text:
        return TurnResult("", sid, False, parser.error or "the agent returned an empty answer",
                          steps=parser.steps)
    return TurnResult(text, sid, True, steps=parser.steps)


def _human_s(s: float) -> str:
    s = int(s)
    if s >= 3600 and s % 3600 == 0:
        return f"{s // 3600} h"
    if s >= 60 and s % 60 == 0:
        return f"{s // 60} min"
    return f"{s} s"


async def probe_model(provider: str, model: str, effort: str = "", *, cwd: str | os.PathLike,
                      timeout_s: float = 120) -> tuple[bool, str]:
    """One real, tiny request with exactly the flags a turn would use. A model id is only a string until
    the API answers to it; nothing should be saved before this passes."""
    res = await run_turn(provider, "Reply with exactly: OK", None, cwd=cwd, model=model, effort=effort,
                         limits=Limits(silence_s=timeout_s, hard_s=timeout_s))
    return res.ok, ("" if res.ok else (res.error or "no answer"))
