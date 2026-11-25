"""Telegram topics as conversations: lifecycle events, learning unknown topics, deletion detection.

Telegram sends events for created / edited / closed / reopened topics, but nothing when a topic is deleted,
and there is no method to list topics. Deletion is therefore detected two ways:
  * a send to the topic fails with TOPIC_ID_INVALID / "message thread not found";
  * a periodic probe: editForumTopic(chat, thread, name=<stored name>). Alive: ok (private chat) or 400
    TOPIC_NOT_MODIFIED (group). Deleted: 400 TOPIC_ID_INVALID. It produces no service message.
The probe renames the topic to the stored name, so it only runs when that name is current: confirmed by a
live event or set by us. Names learned from an old message may be stale and are never used for it.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from core.conversations import Conversation, Registry, make_key

from .api import TgError

SERVICE_KEYS = ("forum_topic_created", "forum_topic_edited", "forum_topic_closed", "forum_topic_reopened",
                "general_forum_topic_hidden", "general_forum_topic_unhidden")
TITLE_MAX = 48


def chat_kind(chat: dict) -> str:
    return "dm" if chat.get("type") == "private" else "group"


def thread_of(msg: dict) -> int | None:
    """Thread id of a message, or None for the part of the chat outside any topic ("main").
    In groups without topics `message_thread_id` also marks reply threads, hence is_topic_message."""
    if msg.get("is_topic_message") and msg.get("message_thread_id"):
        return int(msg["message_thread_id"])
    return None


def key_of(msg: dict) -> str:
    return make_key("tg", msg["chat"]["id"], thread_of(msg))


def is_service(msg: dict) -> bool:
    return any(k in msg for k in SERVICE_KEYS)


def title_from_text(text: str) -> str:
    """A short topic name from the first message: first line, whole words, at most TITLE_MAX chars."""
    line = " ".join((text or "").strip().split("\n", 1)[0].split())
    if len(line) <= TITLE_MAX:
        return line
    cut = line[:TITLE_MAX].rsplit(" ", 1)[0] or line[:TITLE_MAX]
    return cut.rstrip(",.;:-") + "…"


class Topics:
    def __init__(self, api, registry: Registry, notify_main: Callable[[int, str], Awaitable[None]],
                 log=print, clock=time.time):
        self.api = api
        self.registry = registry
        self.notify_main = notify_main
        self.log = log
        self.clock = clock

    # ------------------------------------------------------------------ learning
    def learn(self, msg: dict) -> Conversation:
        """Make sure the conversation of this message exists and pick up what the message reveals."""
        chat = msg["chat"]
        key = key_of(msg)
        thread = thread_of(msg)
        conv = self.registry.get(key)
        name, implicit = None, False
        reply = msg.get("reply_to_message") or {}
        created = reply.get("forum_topic_created")
        if thread is not None and created and reply.get("message_id") == thread:
            name, implicit = created.get("name"), bool(created.get("is_name_implicit"))
        if conv is None:
            fields = dict(kind=chat_kind(chat), state="open")
            if thread is None:
                fields["title"] = "main"
                fields["title_confirmed"] = False
            elif name:
                # Learned from the topic's creation message: the name it was created with. It may have
                # been renamed since, so it is NOT confirmed and is never used for probing.
                fields.update(title=name, title_confirmed=False, name_implicit=implicit)
            conv = self.registry.upsert(key, **fields)
            self.log("conversation learned", key, repr(conv.title))
        elif conv.state == "deleted":
            # A message in it proves it exists (thread ids are never reused): the detection was wrong.
            conv = self.registry.upsert(key, state="open", deleted_at=0.0)
            self.log("conversation revived", key)
        elif not conv.title and name:
            conv = self.registry.upsert(key, title=name, name_implicit=implicit)
        return conv

    async def on_service(self, msg: dict) -> None:
        chat = msg["chat"]
        key = key_of(msg)
        kind = chat_kind(chat)
        if "forum_topic_created" in msg:
            t = msg["forum_topic_created"] or {}
            self.registry.upsert(key, title=t.get("name", ""), title_confirmed=True,
                                 name_implicit=bool(t.get("is_name_implicit")), kind=kind, state="open",
                                 deleted_at=0.0)
            self.log("topic created", key, repr(t.get("name")))
        elif "forum_topic_edited" in msg:
            t = msg["forum_topic_edited"] or {}
            if t.get("name"):
                self.registry.upsert(key, title=t["name"], title_confirmed=True, name_implicit=False,
                                     kind=kind)
                self.log("topic renamed", key, repr(t["name"]))
        elif "forum_topic_closed" in msg:
            self.registry.upsert(key, state="closed", kind=kind)
            self.log("topic closed", key)
        elif "forum_topic_reopened" in msg:
            self.registry.upsert(key, state="open", kind=kind)
            self.log("topic reopened", key)
        elif "general_forum_topic_hidden" in msg:
            self.registry.update_chat(chat["id"], general_hidden=True)
        elif "general_forum_topic_unhidden" in msg:
            self.registry.update_chat(chat["id"], general_hidden=False)

    def on_my_chat_member(self, upd: dict, owner_id: int | None) -> None:
        chat = upd.get("chat") or {}
        new = upd.get("new_chat_member") or {}
        by_owner = (upd.get("from") or {}).get("id") == owner_id
        meta = {"title": chat.get("title", ""), "type": chat.get("type", ""), "is_forum": bool(chat.get("is_forum")),
                "status": new.get("status", ""),
                "can_manage_topics": bool(new.get("can_manage_topics")) or new.get("status") == "creator"}
        if by_owner:
            meta["trusted"] = True
        self.registry.update_chat(chat.get("id"), **meta)
        self.log("membership", chat.get("id"), meta["status"], "topics" if meta["can_manage_topics"] else "")

    # ------------------------------------------------------------------ deletion
    async def gone(self, key: str, reason: str) -> bool:
        """Mark a topic deleted, archive it and tell the chat. True if it was newly marked."""
        conv = self.registry.mark_deleted(key, reason=reason)
        if conv is None:
            return False
        self.log("topic deleted", key, reason)
        try:
            await self.notify_main(conv.chat_id, f"🗑 Topic «{conv.display_title()}» was deleted, so its "
                                                 "conversation is archived.")
        except Exception as e:   # noqa: BLE001 - a notice must never break the door
            self.log("deletion notice failed", key, repr(e))
        return True

    def probe_allowed(self, conv: Conversation) -> bool:
        if conv.is_main or conv.state == "deleted" or not conv.title or not conv.title_confirmed:
            return False
        if conv.kind == "group" and self.registry.chat(conv.chat_id).get("can_manage_topics") is False:
            return False
        return True

    async def probe(self, conv: Conversation) -> str:
        """-> "alive" | "deleted" | "unknown" | "skipped"."""
        if not self.probe_allowed(conv):
            return "skipped"
        try:
            await self.api.call("editForumTopic", chat_id=conv.chat_id, message_thread_id=conv.thread_id,
                                name=conv.title)
            result = "alive"
        except TgError as e:
            if e.topic_gone:
                result = "deleted"
            elif e.not_modified:
                result = "alive"
            else:
                self.log("probe inconclusive", conv.key, e.description)
                result = "unknown"
        if result == "deleted":
            await self.gone(conv.key, "probe")
        else:
            self.registry.upsert(conv.key, last_probe_at=self.clock())
        return result

    async def probe_due(self, interval: float, pause: float = 1.0) -> dict[str, str]:
        now = self.clock()
        results = {}
        for conv in self.registry.all(door="tg"):
            if not self.probe_allowed(conv) or now - conv.last_probe_at < interval:
                continue
            results[conv.key] = await self.probe(conv)
            if pause:
                await asyncio.sleep(pause)
        return results

    async def probe_loop(self, interval: float, first_delay: float = 30.0) -> None:
        await asyncio.sleep(first_delay)
        while True:
            try:
                await self.probe_due(interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log("probe loop error", repr(e))
            await asyncio.sleep(min(60.0, max(5.0, interval / 10)))

    # ------------------------------------------------------------------ actions
    async def rename(self, conv: Conversation, name: str) -> bool:
        name = name.strip()[:128]
        if conv.is_main or not name:
            return False
        try:
            await self.api.call("editForumTopic", chat_id=conv.chat_id, message_thread_id=conv.thread_id,
                                name=name)
        except TgError as e:
            if e.topic_gone:
                await self.gone(conv.key, "rename")
                return False
            if not e.not_modified:
                self.log("rename failed", conv.key, e.description)
                return False
        self.registry.upsert(conv.key, title=name, title_confirmed=True, name_implicit=False)
        return True
