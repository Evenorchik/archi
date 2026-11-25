"""The conversation digest a review reads, built from the turn log (core/learnlog.py).

Recent turns are kept nearly verbatim, older ones collapsed; tool outputs never were in the log, only the
one-line step labels. The owner's text gets the larger budget because corrections live there. Every turn
shows when it happened (the reviewer turns "tomorrow" into a date), a new session is marked, turns the door
started are labelled so they are never mistaken for the owner, and anything that looks like a secret is
removed before the reviewer sees it.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.learnlog import STAMP_RE  # noqa: E402
from tools.learn.stores import redact  # noqa: E402


def _cut(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + " …(cut)"


def _when(ts, tz=None) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def build(turns: list[dict], *, max_turns: int = 40, budget: int = 60000, recent_turns: int = 12,
          tz=None) -> dict:
    """-> {"text", "turns", "shown", "tool_calls", "first_seq", "last_seq"} for `turns` (oldest first)."""
    shown = turns[-max_turns:] if max_turns > 0 else turns
    lines: list[str] = []
    if len(turns) > len(shown):
        lines.append(f"…({len(turns) - len(shown)} older turn(s) since the last review are not shown)…")
    prev_session = None
    for i, t in enumerate(shown):
        recent = i >= len(shown) - recent_turns
        u_cap, a_cap = (2500, 2000) if recent else (300, 200)
        session = t.get("session") or ""
        if prev_session and session and session != prev_session:
            lines.append("— a new session starts here: the agent no longer saw the turns above —")
        prev_session = session or prev_session
        who = "DOOR (an automatic message from the door, not the owner)" if t.get("from_door") else "OWNER"
        prompt = STAMP_RE.sub("", str(t.get("prompt") or ""), count=1)
        lines.append(f"### {who} · {_when(t.get('ts'), tz)}\n{_cut(redact(prompt), u_cap) or '(no text)'}")
        steps = [str(s) for s in t.get("steps") or []]
        if steps:
            listed = steps if recent else steps[:6]
            more = f" … +{len(steps) - len(listed)}" if len(listed) < len(steps) else ""
            lines.append(f"[steps ×{len(steps)}: " + "; ".join(_cut(s, 80) for s in listed) + more + "]")
        answer = str(t.get("answer") or "")
        if answer.strip():
            lines.append(f"### AGENT\n{_cut(redact(answer), a_cap)}")
    text = "\n\n".join(lines)
    if len(text) > budget:
        text = "…(older part trimmed)…\n\n" + text[-budget:]
    return {"text": text, "turns": len(turns), "shown": len(shown),
            "tool_calls": sum(int(t.get("tool_calls") or 0) for t in turns),
            "first_seq": int(turns[0].get("seq") or 0) if turns else 0,
            "last_seq": int(turns[-1].get("seq") or 0) if turns else 0}
