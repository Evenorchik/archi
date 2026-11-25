"""Live status of one running turn, independent of any door.

The engine's events are fed in (`feed`); a door renders the model however its platform allows (a Telegram
draft, an edited status message, a Matrix notice). Plain text only here; doors escape/format it.
"""
from __future__ import annotations

import time

from .engine import Event, TurnResult


def clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class TurnStatus:
    def __init__(self, now=time.monotonic):
        self._now = now
        self.t0 = now()
        self.steps: list[str] = []
        self.preview = ""                     # assistant text so far
        self.background: dict[str, str] = {}  # background command id -> label
        self.state = "running"                # running | done | failed | cancelled
        self.error = ""
        self.ended_at: float | None = None
        self.version = 0                      # bumped on every visible change

    # ------------------------------------------------------------------ input
    def feed(self, ev: Event) -> None:
        if ev.kind == "step" and ev.text:
            self.steps.append(ev.text)
        elif ev.kind == "text":
            if ev.text == self.preview:
                return
            self.preview = ev.text
        elif ev.kind == "bg":
            bid = str(ev.data.get("id", ""))
            if ev.data.get("state") == "running":
                self.background[bid] = ev.text
            else:
                self.background.pop(bid, None)
        elif ev.kind == "done" and ev.result is not None:
            self.finish(ev.result)
            return
        else:
            return
        self.version += 1

    def finish(self, result: TurnResult) -> None:
        if self.state != "running":
            return
        self.state = "cancelled" if result.cancelled else ("done" if result.ok else "failed")
        self.error = result.error
        self.ended_at = self._now()
        self.version += 1

    # ------------------------------------------------------------------ output
    @property
    def elapsed(self) -> float:
        return (self.ended_at or self._now()) - self.t0

    @property
    def current(self) -> str:
        return self.steps[-1] if self.steps else ""

    def headline(self) -> str:
        n = len(self.steps)
        steps = f" · {n} step{'s' if n != 1 else ''}" if n else ""
        return f"Working · {clock(self.elapsed)}{steps}"

    def step_lines(self, previous: int = 2) -> list[str]:
        """Current step first, then up to `previous` earlier ones (newest first)."""
        if not self.steps:
            return ["thinking"]
        return [self.steps[-1], *self.steps[-1 - previous:-1][::-1]]

    def footer(self) -> str:
        n = len(self.steps)
        steps = f" · {n} step{'s' if n != 1 else ''}" if n else ""
        word = {"done": "Done in", "cancelled": "Stopped after", "failed": "Failed after"}.get(
            self.state, "Working for")
        return f"{word} {clock(self.elapsed)}{steps}"

    def render_text(self, previous: int = 2) -> str:
        lines = [self.headline()]
        cur, *prev = self.step_lines(previous)
        lines.append(f"→ {cur}")
        lines += [f"· {p}" for p in prev]
        if self.background:
            lines.append("in background: " + "; ".join(self.background.values()))
        return "\n".join(lines)
