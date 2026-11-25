"""Live status of a running turn in Telegram.

* Private chat: sendMessageDraft streams an ephemeral preview (status + the answer so far) with a stop
  button. A draft lives about 30 s, so it is refreshed while the turn runs; the final answer is a normal
  sendMessage, which replaces the draft. Pressing stop sends a `stopped_message_generation` update.
* Group (drafts are private-chat only), or when drafts fail: one status message per turn, edited at most
  every EDIT_MIN_S, deleted when the turn succeeded, turned into a short footer otherwise.
Every call here is best effort: a failing status display must never break or slow down the turn.
"""
from __future__ import annotations

import asyncio
import random
import time

from core.status import TurnStatus

from . import render
from .api import TgError

DRAFT_MIN_S = 1.2        # between draft updates
DRAFT_KEEPALIVE_S = 8    # drafts disappear after ~30 s without an update
STATUS_DELAY_S = 3       # quick answers in groups get no status message at all
EDIT_MIN_S = 2.0         # never edit the status message more often than this
TICK_S = 10              # refresh the clock even when nothing happened
TYPING_S = 5
PREVIEW_MAX = 3000


def status_html(st: TurnStatus, with_preview: bool) -> str:
    cur, *prev = st.step_lines(2)
    lines = [f"<i>⏳ {render.escape(st.headline())}</i>", f"→ {render.escape(cur)}"]
    lines += [f"· {render.escape(p)}" for p in prev]
    if st.background:
        lines.append("in background: " + render.escape("; ".join(st.background.values())))
    text = "\n".join(lines)
    if with_preview and st.preview.strip():
        tail = st.preview[-PREVIEW_MAX:]
        if len(st.preview) > PREVIEW_MAX:
            tail = "…" + tail.split("\n", 1)[-1] if "\n" in tail else "…" + tail
        body = render.to_html(tail)
        if render.visible_len(body) + render.visible_len(text) > 4000:
            body = render.escape(tail[-1500:])
        text = f"{text}\n\n{body}"
    return text


class LiveStatus:
    def __init__(self, api, chat_id: int, thread_id: int | None, kind: str, status: TurnStatus, log=print):
        self.api = api
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.mode = "draft" if kind == "dm" else "message"
        self.st = status
        self.log = log
        self.draft_id = random.randint(1, 2**31 - 1)
        self.msg_id: int | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._typing: asyncio.Task | None = None
        self._shown_version = -1
        self._last_send = 0.0
        self._plain_preview = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.mode == "draft":
            await self._draft("")          # "Thinking…" placeholder right away
        self._task = asyncio.create_task(self._loop())
        if self.mode == "message":
            self._typing = asyncio.create_task(self._typing_loop())

    def poke(self) -> None:
        self._wake.set()

    async def finish(self) -> None:
        """The turn ended: stop updating (the final state is shown by done(), after the delivery)."""
        for t in (self._task, self._typing):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    async def done(self, ok: bool, outcome: str = "delivered") -> None:
        """After the delivery: the status message goes when the owner has the answer, else it becomes a footer
        that says what happened."""
        if self.msg_id is None:
            return
        try:
            if ok and outcome == "delivered":
                await self.api.call("deleteMessage", chat_id=self.chat_id, message_id=self.msg_id)
            else:
                line = self.st.footer()
                if outcome == "queued":
                    line += " · the answer could not be delivered yet; it is sent as soon as Telegram answers again"
                await self.api.call("editMessageText", chat_id=self.chat_id, message_id=self.msg_id,
                                    text=f"<i>{render.escape(line)}</i>", parse_mode="HTML")
        except TgError as e:
            self.log("status cleanup failed", e.description)

    async def stop(self, ok: bool) -> None:
        await self.finish()
        await self.done(ok, "delivered" if ok else "failed")

    # ------------------------------------------------------------------ loops
    async def _loop(self) -> None:
        try:
            if self.mode == "message":
                await asyncio.sleep(STATUS_DELAY_S)
            while True:
                gap = DRAFT_MIN_S if self.mode == "draft" else EDIT_MIN_S
                keep = DRAFT_KEEPALIVE_S if self.mode == "draft" else TICK_S
                since = time.monotonic() - self._last_send
                if since < gap:
                    await asyncio.sleep(gap - since)
                changed = self.st.version != self._shown_version
                if changed or time.monotonic() - self._last_send >= keep or self.msg_id is None:
                    await self._show()
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=keep)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _typing_loop(self) -> None:
        try:
            while True:
                try:
                    await self.api.call("sendChatAction", chat_id=self.chat_id, action="typing",
                                        message_thread_id=self.thread_id)
                except TgError:
                    pass
                await asyncio.sleep(TYPING_S)
        except asyncio.CancelledError:
            pass

    async def _show(self) -> None:
        version = self.st.version
        if self.mode == "draft":
            ok = await self._draft(status_html(self.st, with_preview=True))
            if not ok:
                self.log("drafts unavailable here, falling back to a status message")
                self.mode = "message"
                self._typing = self._typing or asyncio.create_task(self._typing_loop())
                return
        else:
            await self._message(status_html(self.st, with_preview=False))
        self._shown_version = version
        self._last_send = time.monotonic()

    async def _draft(self, text: str) -> bool:
        try:
            await self.api.call("sendMessageDraft", chat_id=self.chat_id, draft_id=self.draft_id,
                                message_thread_id=self.thread_id, text=text,
                                parse_mode="HTML" if text else None, can_stop=True)
            return True
        except TgError as e:
            if e.bad_markup and text:
                try:
                    await self.api.call("sendMessageDraft", chat_id=self.chat_id, draft_id=self.draft_id,
                                        message_thread_id=self.thread_id, text=render.strip_html(text)[:4000],
                                        can_stop=True)
                    return True
                except TgError:
                    pass
            self.log("draft failed", e.description)
            return e.retry_after is not None   # flood control is temporary; anything else: give up drafts

    async def _message(self, text: str) -> None:
        try:
            if self.msg_id is None:
                res = await self.api.call("sendMessage", chat_id=self.chat_id, message_thread_id=self.thread_id,
                                          text=text, parse_mode="HTML", disable_notification=True)
                self.msg_id = (res or {}).get("message_id")
            else:
                await self.api.call("editMessageText", chat_id=self.chat_id, message_id=self.msg_id, text=text,
                                    parse_mode="HTML")
        except TgError as e:
            if not e.not_modified:
                self.log("status message failed", e.description)
