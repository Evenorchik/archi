"""After a turn: the hook between the doors and the self-learning loop (tools/learn).

CONTRACT (doors call these; keep the signatures):
  turn_finished(door, key, provider=, session_id=, tool_calls=, home=, prompt=, answer=, steps=) -> str | None
      Called by a door right after the answer of a turn was DELIVERED (never before: a review must not
      compete with the turn it reviews). `prompt` is what the agent was given, without the door's timestamp
      line (the turn log stores its own time), `answer` the final answer as delivered, `steps` the visible
      step labels. This
      is the provider-independent record the learning loop reviews (no Claude/Codex transcript parsing).
      Counts the turn for conversation `key`; when a threshold is crossed it starts a detached background
      review and returns its kind ("memory" | "skill" | "combined"), else None. Must return within a second
      and must never raise.
  review_now(door, key, provider=, session_id=, home=) -> str
      The /learn command: start a review of this conversation's session now. Returns a one-line message for
      the owner (started / nothing to review / disabled). Must never raise.

How it works: every turn is appended to the conversation's turn log (core/learnlog.py) and counted. Every
`memory_every_turns` owner turns a memory review starts, every `skill_every_tool_calls` tool calls a skill
review, both at once = "combined". The review (tools/learn/review.py) runs detached (core/detach.py: a
transient systemd unit, nice 10), one at a time, on copies of the memory store and the skills, and applies
what passes validation. Turns the door itself started (job reports, the first-run greeting) are logged and
their tool calls count, but they are not owner turns. Conversations in `exclude_conversations`, and every
conversation while the loop is disabled, are not even logged.
"""
from __future__ import annotations

import logging
import os
import secrets
import sys
import time
from pathlib import Path

from . import detach, learnlog
from .config import CODE_DIR, home as default_home

log = logging.getLogger("archi.learn")

REVIEW_SCRIPT = CODE_DIR / "tools" / "learn" / "review.py"
# Passed on to the review process: where things are and which CLI to use. Never secrets (detach.py).
PASS_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "ARCHI_MEMORY_DIR", "ARCHI_UNIT_PREFIX", "ARCHI_DETACH",
            "CLAUDE_BIN", "CODEX_BIN")


def turn_finished(door: str, key: str, *, provider: str, session_id: str | None, tool_calls: int,
                  home: str | os.PathLike | None = None, prompt: str = "", answer: str = "",
                  steps: list[str] | tuple[str, ...] = ()) -> str | None:
    try:
        return _turn_finished(door, key, provider=provider, session_id=session_id, tool_calls=tool_calls,
                              home=Path(home) if home else default_home(), prompt=prompt or "",
                              answer=answer or "", steps=steps or ())
    except Exception as e:  # noqa: BLE001 - the learning loop must never break a door
        log.warning("learning hook failed for %s: %r", key, e)
        return None


def _turn_finished(door, key, *, provider, session_id, tool_calls, home: Path, prompt, answer, steps):
    cfg = learnlog.load_config(home)
    if not cfg.get("enabled", True) or learnlog.excluded(cfg, key):
        return None
    from_door = learnlog.from_door(prompt)
    entry = learnlog.make_entry(door=door, provider=provider, session_id=session_id, prompt=prompt,
                                answer=answer, steps=steps, tool_calls=tool_calls, from_door=from_door)
    with learnlog.locked(home):
        counters = learnlog.load_counters(home)
        st = counters.get(key) if isinstance(counters.get(key), dict) else {}
        seq = learnlog.append_turn(home, key, entry, last_seq=int(st.get("seq") or 0))
        st["turns"] = int(st.get("turns") or 0) + (0 if from_door else 1)
        st["tools"] = int(st.get("tools") or 0) + entry["tool_calls"]
        st.update(seq=seq, session=session_id or "", provider=provider, door=door, last_turn_at=entry["ts"])
        if not from_door:
            st["last_owner_turn_at"] = entry["ts"]
        every_turns = learnlog.number(cfg, "memory_every_turns")
        every_tools = learnlog.number(cfg, "skill_every_tool_calls")
        mem = every_turns > 0 and st["turns"] >= every_turns
        skl = cfg.get("skill_mode") != "off" and every_tools > 0 and st["tools"] >= every_tools
        kind = "combined" if mem and skl else "memory" if mem else "skill" if skl else None
        counters[key] = st
        learnlog.save_counters(home, counters)
    if not kind:
        return None
    # Started outside the lock, which every conversation's hook needs: a slow systemd-run must not hold up
    # the others. Turns of this conversation cannot run meanwhile (the door awaits this hook).
    started = spawn_review(home, key, kind)
    if not started.ok:
        log.warning("learning review for %s not started: %s", key, started.error)
        return None                    # the counters keep growing, so the next turn tries again
    if started.warning:
        log.warning("learning review for %s: %s", key, started.warning)
    learnlog.reset_counters(home, key, turns=mem, tools=skl)
    return kind


def review_env(home: Path) -> dict:
    env = {k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env["ARCHI_HOME"] = str(home)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def spawn_review(home: str | os.PathLike, key: str, kind: str, *, explicit: bool = False) -> detach.Spawned:
    """Start tools/learn/review.py for one conversation, detached and niced."""
    home = Path(home)
    argv = [sys.executable, str(REVIEW_SCRIPT), "--home", str(home), "--conversation", key, "--kind", kind]
    if explicit:
        argv.append("--explicit")
    name = detach.unit_name("learn", f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}")
    # A short limit: the door waits for this inside the conversation's lock (a hung systemd-run falls back).
    return detach.spawn(argv, name=name, description=f"Archi: learning review ({kind})", env=review_env(home),
                        cwd=str(home), log_path=home / "logs" / "learn.log", nice=10, timeout=5)


def review_now(door: str, key: str, *, provider: str, session_id: str | None,
               home: str | os.PathLike | None = None) -> str:
    try:
        home = Path(home) if home else default_home()
        cfg = learnlog.load_config(home)
        if not cfg.get("enabled", True):
            return "🧠 The learning loop is off. `archi learn enable` turns it back on."
        if learnlog.excluded(cfg, key):
            return "🧠 This conversation is excluded from the learning loop (state/learn/config.json)."
        kind = "memory" if cfg.get("skill_mode") == "off" else "combined"
        n = len(learnlog.pending_turns(home, key, kind))
        if n == 0:
            return "🧠 Nothing new to review here since the last review."
        started = spawn_review(home, key, kind, explicit=True)
        if not started.ok:
            return f"⚠️ Could not start the review: {started.error}"
        learnlog.reset_counters(home, key)
        return (f"🧠 Review started for the last {n} turn{'s' if n != 1 else ''} of this conversation. "
                "A short notice follows here when it is done.")
    except Exception as e:  # noqa: BLE001
        log.warning("review_now failed for %s: %r", key, e)
        return f"⚠️ Could not start the review: {e}"
