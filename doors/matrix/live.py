"""Live status of a running turn in a Matrix room.

* A ⏳ reaction on the owner's message as soon as the turn starts; ✅ (done) or ⚠️ (failed, stopped) replaces it
  at the end.
* After STATUS_DELAY_S one m.notice appears and is edited in place (m.replace) with the steps from the agent's
  tool calls: at most every EDIT_MIN_S, and every TICK_S for the clock when nothing new happens. Notices do not
  notify under Matrix's default push rules, so only the answer (an m.text) does. Quick answers never get a
  notice. At the end it is edited into a one-line footer.
* "typing…" is kept up for the whole turn: servers cap one typing notification to tens of seconds.
Every call here is best effort: a failing status display never breaks or slows down the turn.
"""
from __future__ import annotations

import asyncio
import time

from core.status import TurnStatus

from .render import HTML_FORMAT, escape

STATUS_DELAY_S = 4        # quick answers get only the reactions, no status notice
EDIT_MIN_S = 3.0          # never edit the notice more often than this
TICK_S = 30               # refresh the clock even when nothing happened
TYPING_TTL_MS = 30000     # what one typing notification claims
TYPING_EVERY_S = 20       # renewed before it lapses
ICONS = {"done": "✅", "cancelled": "⏹", "failed": "⚠️"}


def status_text(st: TurnStatus) -> tuple[str, str]:
    """(plain body, HTML) of the running status."""
    cur, *prev = st.step_lines(2)
    plain = [f"⏳ {st.headline()}", f"→ {cur}", *[f"· {p}" for p in prev]]
    rich = [f"<i>⏳ {escape(st.headline())}</i>", f"→ {escape(cur)}", *[f"· {escape(p)}" for p in prev]]
    if st.background:
        bg = "in background: " + "; ".join(st.background.values())
        plain.append(bg)
        rich.append(escape(bg))
    return "\n".join(plain), "<br>".join(rich)


def footer_text(st: TurnStatus) -> tuple[str, str]:
    line = f"{ICONS.get(st.state, '⏳')} {st.footer()}"
    return line, f"<i>{escape(line)}</i>"


class LiveStatus:
    def __init__(self, api, room_id: str, src_event_id: str | None, status: TurnStatus, log=print, *,
                 delay: float = STATUS_DELAY_S, edit_min: float = EDIT_MIN_S, tick: float = TICK_S,
                 typing_every: float = TYPING_EVERY_S, now=time.monotonic):
        self.api = api
        self.room = room_id
        self.src = src_event_id
        self.st = status
        self.log = log
        self.delay, self.edit_min, self.tick, self.typing_every = delay, edit_min, tick, typing_every
        self._now = now
        self.notice_id: str | None = None
        self.reaction_id: str | None = None
        self.edits = 0
        self._shown = -1
        self._last = 0.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._typing: asyncio.Task | None = None

    # ------------------------------------------------------------------ calls (best effort)
    async def _send(self, event_type: str, content: dict) -> str | None:
        try:
            return await self.api.send(self.room, event_type, content)
        except Exception as e:  # noqa: BLE001
            self.log("status send failed", repr(e))
            return None

    async def _react(self, key: str) -> str | None:
        if not self.src:
            return None
        return await self._send("m.reaction", {"m.relates_to": {"rel_type": "m.annotation", "event_id": self.src,
                                                                "key": key}})

    @staticmethod
    def _content(plain: str, rich: str) -> dict:
        return {"msgtype": "m.notice", "body": plain, "format": HTML_FORMAT, "formatted_body": rich}

    async def _show(self, plain: str, rich: str) -> None:
        version = self.st.version
        new = self._content(plain, rich)
        if self.notice_id is None:
            self.notice_id = await self._send("m.room.message", new)
        else:
            await self._send("m.room.message", {
                "msgtype": "m.notice", "body": "* " + plain, "format": HTML_FORMAT, "formatted_body": "* " + rich,
                "m.new_content": new, "m.relates_to": {"rel_type": "m.replace", "event_id": self.notice_id}})
            self.edits += 1
        self._shown = version
        self._last = self._now()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.reaction_id = await self._react("⏳")
        self._task = asyncio.create_task(self._loop())
        self._typing = asyncio.create_task(self._typing_loop())

    def poke(self) -> None:
        self._wake.set()

    async def stop(self, ok: bool) -> None:
        for t in (self._task, self._typing):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            await self.api.typing(self.room, False)
        except Exception:  # noqa: BLE001
            pass
        if self.notice_id is not None:
            await self._show(*footer_text(self.st))
        if self.src:
            if self.reaction_id:
                try:
                    await self.api.redact(self.room, self.reaction_id)
                except Exception as e:  # noqa: BLE001
                    self.log("status reaction not removed", repr(e))
            await self._react("✅" if ok else "⚠️")

    # ------------------------------------------------------------------ loops
    async def _loop(self) -> None:
        try:
            await asyncio.sleep(self.delay)
            await self._show(*status_text(self.st))
            while True:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.tick)
                except asyncio.TimeoutError:
                    pass
                gap = self.edit_min - (self._now() - self._last)
                if gap > 0:
                    await asyncio.sleep(gap)
                if self.st.version != self._shown or self.notice_id is None or self._now() - self._last >= self.tick:
                    await self._show(*status_text(self.st))
        except asyncio.CancelledError:
            pass

    async def _typing_loop(self) -> None:
        try:
            while True:
                try:
                    await self.api.typing(self.room, True, TYPING_TTL_MS)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(self.typing_every)
        except asyncio.CancelledError:
            pass
