"""State of the self-learning loop in <home>/state/learn/, shared by the door hook (core/afterturn.py), the
reviews and the curator (tools/learn/) and `archi learn`.

  config.json        settings; created with the defaults on first use (DEFAULT_CONFIG)
  turns/<key>.jsonl  the turn log, one file per conversation, one line per delivered turn: what the agent was
                     given, the answer as delivered, the visible step labels. Reviews read this log, never a
                     provider's transcript, so they work the same for Claude and Codex. Bounded: the oldest
                     lines are trimmed.
  counters.json      per conversation: owner turns and tool calls since the last review started, the last
                     sequence number, session, provider, time of the last turn
  watermarks.json    per conversation: the sequence number reviewed up to
  runs/<ts>.json     one record per review or curator run
  backups/ stage/ dryrun/   copies made by the runs (pruned to the newest few)

Turns of one conversation never run concurrently (the door serialises them), but different conversations
and the detached reviews do: shared files are changed under one lock (.state.lock) and written atomically.
"""
from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from .config import atomic_write

DEFAULT_CONFIG = {
    "enabled": True,                 # false: nothing is logged, nothing is reviewed (archi learn disable)
    "notify": True,                  # a silent notice in the conversation when a review changed something
    "memory_every_turns": 10,        # owner turns between two memory reviews of a conversation (0 = never)
    "skill_every_tool_calls": 60,    # tool calls between two skill reviews (0 = never)
    "skill_mode": "auto",            # auto: reviews edit skills/ (validated, rolled back on new problems);
                                     # propose: they write proposals to state/learn/proposals/; off: no skills
    "exclude_conversations": [],     # conversation keys or patterns ("tg:-100123:*"): never logged or reviewed
    "provider": "",                  # "" = PROVIDER from config/archi.env
    "model": "",                     # "" = CLAUDE_MODEL / CODEX_MODEL from config/archi.env
    "effort": "",                    # claude reasoning effort for reviews; "" = the model's default
    "timeout_s": 900,                # one reviewer run, hard limit
    "max_turns": 40,                 # turns shown to the reviewer (the most recent ones)
    "digest_chars": 60000,           # size limit of the digest
    "keep_backups": 30,              # memory/skill backups kept under backups/
    "big_skill_lines": 40,           # a skill edit above this many changed lines is flagged as big
}
SKILL_MODES = ("auto", "propose", "off")
KINDS = ("memory", "skill", "combined")
DOOR_MARK = "[Message from the Archi door"      # how a turn the door itself started begins (job reports, …)
# The door's clock line in front of a prompt: "[25.11.2026 14:05, Wednesday · Europe/Berlin]" (core/prompt.stamp).
STAMP_RE = re.compile(r"^\s*\[\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}, [^\]\n]{1,60}\][ \t]*\r?\n?")


def from_door(prompt: str) -> bool:
    """True for a turn the door started, not the owner (its prompt may follow the clock line)."""
    return STAMP_RE.sub("", prompt or "", count=1).lstrip().startswith(DOOR_MARK)

MAX_TEXT = 16000          # characters of a prompt or an answer kept in the turn log
MAX_STEPS = 300           # step labels kept per turn
MAX_STEP = 160            # characters per step label
MAX_LOG_TURNS = 400       # a conversation's log is trimmed to KEEP_LOG_TURNS lines beyond this
KEEP_LOG_TURNS = 300
MAX_LOG_BYTES = 4_000_000
KEEP_RUNS = 300


def learn_dir(home: str | os.PathLike) -> Path:
    return Path(home) / "state" / "learn"


# ---------------------------------------------------------------------------------------------- json files

def jload(path: Path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def jsave(path: Path, data, mode: int = 0o600) -> None:
    atomic_write(Path(path), json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", mode=mode)


@contextmanager
def locked(home: str | os.PathLike):
    """The lock for counters, watermarks, config and the turn logs."""
    d = learn_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".state.lock", "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def review_lock(home: str | os.PathLike, wait_s: float = 0, poll: float = 2.0):
    """One review or curator run at a time. Yields True when the lock is held, False when it could not be
    taken within `wait_s` seconds (the caller skips its run then)."""
    d = learn_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / ".review.lock", "a+")
    got = False
    try:
        end = time.monotonic() + max(0.0, wait_s)
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    break
                time.sleep(poll)
        yield got
    finally:
        if got:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


# ---------------------------------------------------------------------------------------------- config

def load_config(home: str | os.PathLike, *, create: bool = True) -> dict:
    """The settings merged over the defaults. A missing file is created with the defaults. A broken one is
    left alone (the owner may be editing it) and the loop FAILS CLOSED until it is fixed: its exclusions and
    its off switch cannot be read, so nothing is logged or reviewed (`_broken` says why)."""
    path = learn_dir(home) / "config.json"
    raw = None
    broken = ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            broken = f"{path} does not hold a JSON object"
    except FileNotFoundError:
        if create:
            try:
                jsave(path, {**DEFAULT_CONFIG}, mode=0o644)
            except OSError:
                pass
    except (OSError, ValueError) as e:
        broken = f"{path} cannot be read as JSON ({e})"
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update(raw)
    if broken:
        cfg["enabled"] = False
        cfg["_broken"] = broken
    if cfg.get("skill_mode") not in SKILL_MODES:
        cfg["skill_mode"] = DEFAULT_CONFIG["skill_mode"]
    if not isinstance(cfg.get("exclude_conversations"), list):
        cfg["exclude_conversations"] = []
    return cfg


def update_config(home: str | os.PathLike, **changes) -> dict:
    """Change keys of config.json, keeping everything else in it (unknown keys included). A file that cannot
    be parsed is never overwritten (that would silently drop its settings, the exclusions among them):
    ValueError, and the file stays as it is."""
    path = learn_dir(home) / "config.json"
    with locked(home):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = None
        if text is None:
            raw = dict(DEFAULT_CONFIG)
        else:
            try:
                raw = json.loads(text)
            except ValueError as e:
                raise ValueError(f"{path} is not valid JSON ({e}); nothing was changed: fix the file (or move "
                                 "it away to start from the defaults) and run the command again") from None
            if not isinstance(raw, dict):
                raise ValueError(f"{path} does not hold a JSON object; nothing was changed")
        raw.update(changes)
        jsave(path, raw, mode=0o644)
    return load_config(home)


def number(cfg: dict, key: str) -> float:
    try:
        return float(cfg.get(key, DEFAULT_CONFIG.get(key, 0)))
    except (TypeError, ValueError):
        return float(DEFAULT_CONFIG.get(key, 0) or 0)


def excluded(cfg: dict, key: str) -> bool:
    return any(fnmatch.fnmatchcase(key, str(p)) for p in cfg.get("exclude_conversations") or [])


# ---------------------------------------------------------------------------------------------- turn log

def split_lines(text: str) -> list[str]:
    """Lines of a JSONL file: split on "\n" only. str.splitlines() also splits on U+2028, U+2029 and U+0085,
    which json.dumps(ensure_ascii=False) leaves raw inside strings, and the turn would be lost."""
    lines = (text or "").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [x[:-1] if x.endswith("\r") else x for x in lines]


def log_name(key: str) -> str:
    """File name of a conversation's log: readable, and unique even for keys that differ only in characters
    a file name cannot hold."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:80]
    return f"{safe}-{hashlib.sha1(key.encode()).hexdigest()[:8]}.jsonl"


def log_path(home: str | os.PathLike, key: str) -> Path:
    return learn_dir(home) / "turns" / log_name(key)


def _cut(text, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[:n] + "\n…(cut)"


def make_entry(*, door: str, provider: str, session_id: str | None, prompt: str, answer: str, steps,
               tool_calls: int, from_door: bool, now: float | None = None) -> dict:
    steps = [" ".join(str(s).split())[:MAX_STEP] for s in list(steps or [])[:MAX_STEPS]]
    try:
        calls = max(0, int(tool_calls or 0))
    except (TypeError, ValueError):
        calls = len(steps)
    return {"ts": time.time() if now is None else now, "door": door, "provider": provider,
            "session": session_id or "", "from_door": bool(from_door), "prompt": _cut(prompt, MAX_TEXT),
            "answer": _cut(answer, MAX_TEXT), "steps": steps, "tool_calls": calls}


def _last_seq(path: Path) -> int:
    """Sequence number of the last complete line of a log. Reads the tail, and more of it when the last
    line is longer than the window (one turn can be a few hundred KB)."""
    window = 65536
    while True:
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - window))
                lines = split_lines(fh.read().decode("utf-8", "replace"))
        except OSError:
            return 0
        if size > window:
            lines = lines[1:]          # the window starts inside a line
        for line in reversed(lines):
            try:
                return int(json.loads(line).get("seq") or 0)
            except (ValueError, AttributeError, TypeError):
                continue
        if size <= window:
            return 0
        window *= 4


def append_turn(home: str | os.PathLike, key: str, entry: dict, last_seq: int = 0) -> int:
    """Append one turn to the conversation's log; the caller holds `locked(home)`. Returns its sequence
    number (one more than the last one, whatever the counters say, so a lost counters file never makes new
    turns look already reviewed)."""
    path = log_path(home, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    seq = max(int(last_seq or 0), _last_seq(path)) + 1
    line = json.dumps({"seq": seq, "key": key, **entry}, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        if path.stat().st_size > MAX_LOG_BYTES or seq % 25 == 0:
            trim(path)
    except OSError:
        pass
    return seq


def trim(path: Path, max_turns: int | None = None, keep: int | None = None, max_bytes: int | None = None) -> bool:
    """Bound a log: beyond `max_turns` lines (or `max_bytes`) keep only the newest `keep` lines that fit in
    three quarters of `max_bytes`. Returns True when the file was rewritten."""
    max_turns = MAX_LOG_TURNS if max_turns is None else max_turns
    keep = KEEP_LOG_TURNS if keep is None else keep
    max_bytes = MAX_LOG_BYTES if max_bytes is None else max_bytes
    try:
        lines = [x + "\n" for x in split_lines(path.read_text(encoding="utf-8", errors="replace"))]
    except OSError:
        return False
    size = sum(len(x.encode("utf-8")) for x in lines)
    if len(lines) <= max_turns and size <= max_bytes:
        return False
    kept: list[str] = []
    budget = max_bytes * 3 // 4
    for line in reversed(lines[-keep:]):
        n = len(line.encode("utf-8"))
        if kept and budget - n < 0:
            break
        budget -= n
        kept.append(line)
    atomic_write(path, "".join(reversed(kept)), mode=0o600)
    return True


def read_turns(home: str | os.PathLike, key: str, after_seq: int = 0) -> list[dict]:
    """Turns of a conversation with a sequence number above `after_seq`, oldest first. Unreadable lines (a
    write cut short) are skipped."""
    out = []
    try:
        text = log_path(home, key).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in split_lines(text):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and int(entry.get("seq") or 0) > after_seq:
            out.append(entry)
    return out


# ---------------------------------------------------------------------------------------------- counters

def counters_path(home) -> Path:
    return learn_dir(home) / "counters.json"


def watermarks_path(home) -> Path:
    return learn_dir(home) / "watermarks.json"


def load_counters(home) -> dict:
    return jload(counters_path(home), {})


def save_counters(home, data: dict) -> None:
    jsave(counters_path(home), data)


WM_KINDS = ("memory", "skill")


def _wm_entry(data: dict, key: str) -> dict:
    """{"memory": seq, "skill": seq}: memory and skill reviews keep their own watermark, so turns a frequent
    memory review has seen are still in the next skill review (a plain number counts for both)."""
    raw = data.get(key)
    if isinstance(raw, dict):
        out = {}
        for k in WM_KINDS:
            try:
                out[k] = int(raw.get(k) or 0)
            except (TypeError, ValueError):
                out[k] = 0
        return out
    try:
        n = int(raw or 0)
    except (TypeError, ValueError):
        n = 0
    return {k: n for k in WM_KINDS}


def watermark(home, key: str, kind: str = "combined") -> int:
    """The sequence number reviewed up to: for one kind, or for both ("combined": the older of the two)."""
    entry = _wm_entry(jload(watermarks_path(home), {}), key)
    return entry[kind] if kind in WM_KINDS else min(entry.values())


def set_watermark(home, key: str, seq: int, kind: str = "combined") -> None:
    with locked(home):
        data = jload(watermarks_path(home), {})
        entry = _wm_entry(data, key)
        last = _last_seq(log_path(home, key))
        for k in (kind,) if kind in WM_KINDS else WM_KINDS:
            # A watermark beyond the end of the log belongs to a log that was reset: replace it, or every later
            # review would start from the beginning again.
            entry[k] = int(seq) if entry[k] > last else max(int(seq), entry[k])
        data[key] = entry
        jsave(watermarks_path(home), data)


def pending_turns(home, key: str, kind: str = "combined") -> list[dict]:
    """Turns a review of `kind` has not seen yet ("combined": not seen by one of the two). A watermark
    beyond the end of the log (the log was reset) counts as none."""
    wm = watermark(home, key, kind)
    if wm and wm > _last_seq(log_path(home, key)):
        wm = 0
    return read_turns(home, key, after_seq=wm)


def conversations(home) -> list[str]:
    """Conversations that have a turn log."""
    keys = set(load_counters(home))
    d = learn_dir(home) / "turns"
    if d.is_dir():
        for p in d.glob("*.jsonl"):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    first = fh.readline()
                key = json.loads(first).get("key")
            except (OSError, ValueError, AttributeError):
                continue
            if key:
                keys.add(str(key))
    return sorted(keys)


def is_private(home, key: str) -> bool:
    """True for a conversation only the owner and the agent are in: a Telegram private chat (positive chat
    id), a Matrix room the door counted as two members (kind "dm"). Unknown means no."""
    try:
        from .conversations import Registry, parse_key
        conv = Registry(Path(home) / "state").get(key)
        door, chat, _thread = parse_key(key)
    except Exception:  # noqa: BLE001 - an unreadable registry or key is simply not private
        return False
    if conv is None or conv.state == "deleted" or conv.kind != "dm":
        return False
    if door == "tg":
        try:
            return int(chat) > 0
        except ValueError:
            return False
    return door == "mx"


def most_recent_conversation(home, cfg: dict | None = None, *, private_only: bool = False) -> str | None:
    """The conversation the owner used last (not excluded; with `private_only`, only one nobody else reads):
    where reports without a conversation of their own go."""
    cfg = cfg or load_config(home, create=False)
    best, best_t = None, -1.0
    for key, st in load_counters(home).items():
        if excluded(cfg, key) or not isinstance(st, dict):
            continue
        if private_only and not is_private(home, key):
            continue
        t = float(st.get("last_owner_turn_at") or st.get("last_turn_at") or 0)
        if t > best_t:
            best, best_t = key, t
    return best


def reset_counters(home, key: str, *, turns: bool = True, tools: bool = True) -> None:
    with locked(home):
        data = load_counters(home)
        st = data.get(key) or {}
        if turns:
            st["turns"] = 0
        if tools:
            st["tools"] = 0
        st["last_review_started_at"] = time.time()
        data[key] = st
        save_counters(home, data)


# ---------------------------------------------------------------------------------------------- runs

def run_stamp() -> str:
    """A unique, sortable name for one run: local time plus pid."""
    return time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"


def record_run(home, name: str, record: dict) -> Path:
    d = learn_dir(home) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    jsave(path, record, mode=0o600)
    prune(d, KEEP_RUNS)
    return path


def recent_runs(home, n: int = 5) -> list[dict]:
    d = learn_dir(home) / "runs"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]:
        rec = jload(p, {})
        if rec:
            rec.setdefault("name", p.stem)
            out.append(rec)
    return out


def prune(d: Path, keep: int, protect: tuple[str, ...] | list[str] = ()) -> None:
    """Keep the newest `keep` entries of a directory (by modification time); never the ones in `protect`."""
    if not d.is_dir():
        return
    try:
        entries = sorted((p for p in d.iterdir() if p.name not in protect), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    keep = max(0, keep - len([n for n in protect if (d / n).exists()]))
    for old in entries[:-keep] if keep > 0 else entries:
        if old.name.startswith("."):
            continue
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink(missing_ok=True)
