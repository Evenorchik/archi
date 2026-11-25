"""The Matrix door: Element (or any Matrix client) rooms as conversations, turns through core.engine.

Run: `python -m doors.matrix` (reads config/archi.env under ARCHI_HOME); `--check` is the dry start of
`archi validate`.

* Every room the bot is in is one conversation, key `mx:<room_id>`, with its own session; rooms run in
  parallel. The room name is the conversation's title. A room the bot is kicked from or leaves, and a room
  the owner leaves (the bot then leaves too), is archived like a deleted Telegram topic.
* Only MATRIX_OWNER_ID is answered and only the owner's invites are accepted; other invites are rejected and
  everybody else is ignored, silently. The door keeps every room's members (from the sync, or one /members
  call for a room it has not seen whole) and never posts into a room the owner is not in; it leaves such a
  room. A finished job whose room is gone reports only in a private room of the owner and the bot, made for
  it when there is none.
* No end-to-end encryption (matrix-nio without its e2e extra), so an encrypted room cannot be read. The bot
  never stays in one: invited into an encrypted room, or when encryption is switched on in a room it is in, it
  says so once in plain text and leaves. `archi status` counts those rooms.
* The sync token is kept in state/matrix.json: a restart neither answers a message twice nor misses one that
  arrived while the door was down (up to MAX_MESSAGE_AGE_S old). The first start ever skips the history.
* Sends that fail for a passing reason are retried; an answer that still could not go out waits in the outbox
  (core.door) and is delivered when the homeserver answers again, its ⚠️ then turned into ✅.
* Commands work with "!" (Element keeps "/" for its own commands and asks before sending an unknown one) and
  with "/".

The door handles the event JSON of the sync response as the spec documents it, so its logic is testable with
plain dicts; only api.MxApi touches matrix-nio. Turns, delivery, the shared commands and the learning and jobs
contracts are core.door.BaseDoor.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

from core import voice
from core.config import Config, atomic_write
from core.conversations import Conversation, Registry, make_key, parse_key
from core.door import BaseDoor, SendFailed, command_list
from core.inbox import file_prompt, inbox_path
from core.prompt import base_system_prompt
from core.status import TurnStatus

from . import media, render
from .api import MxError, is_mxid, txn_id
from .live import LiveStatus

log_ = logging.getLogger("archi.matrix")

DOOR = "matrix"
COMMANDS = command_list("room")
SYNC_TIMEOUT_MS = 30000
MAX_MESSAGE_AGE_S = 24 * 3600     # an owner message older than this (the door was down) is not answered
MIN_FREE_BYTES = 512 * 1024 * 1024
JOIN_RETRY_S = (5, 15, 60, 300, 900)   # a failed join of the owner's invite (or a failed leave) is retried this long
IN_ROOM = ("join", "invite")
SYNC_FILTER = {"presence": {"types": []}, "account_data": {"types": []},
               "room": {"timeline": {"limit": 50}, "ephemeral": {"types": []}, "account_data": {"types": []}}}
GREETING = "👋 I am here. This room is a separate conversation with its own session: write to me here."
ENCRYPTED_NOTICE = ("🔒 This room is end-to-end encrypted, and I cannot read encrypted messages, so I am leaving it. "
                    "Create a room with encryption switched off (in Element: New room, then turn off \"Enable "
                    "end-to-end encryption\") and invite me there.")
REPORT_ROOM = "{name}: background jobs"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_UKRAINIAN = re.compile(r"[ІіЇїЄєҐґ]")
_COMMAND = re.compile(r"^[!/]([A-Za-z]+)(?:\s+(.*))?$", re.S)


class DoorFatal(RuntimeError):
    """The door cannot work at all (rejected access token, wrong account): it stops."""


def room_of(key: str) -> str:
    return parse_key(key)[1]


def strip_reply_fallback(body: str) -> str:
    """Older clients quote the message they reply to at the top of `body` ("> <@user> text", blank line)."""
    lines = (body or "").split("\n")
    if not lines or not lines[0].startswith("> <@"):
        return body or ""
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    return "\n".join(lines[i:]).lstrip("\n")


def is_state(ev: dict, event_type: str) -> bool:
    """A state event of that type for the room itself (state_key ""): a timeline event that only carries
    the type, without a state key, changes nothing."""
    return ev.get("type") == event_type and ev.get("state_key") == ""


def _state_value(events: list, event_type: str, field: str) -> str | None:
    value = None
    for ev in events:
        if is_state(ev, event_type):
            value = str((ev.get("content") or {}).get(field) or "")
    return value


def config_problem(cfg: Config) -> str | None:
    hs = cfg.get("MATRIX_HOMESERVER").strip()
    if not hs:
        return f"MATRIX_HOMESERVER is not set in {cfg.path} (run `archi setup matrix`)"
    if not hs.startswith(("http://", "https://")):
        return f"MATRIX_HOMESERVER={hs!r} is not an http(s) URL"
    if not is_mxid(cfg.get("MATRIX_USER_ID")):
        return "MATRIX_USER_ID is not a Matrix user id (@name:server); run `archi setup matrix`"
    if not cfg.get("MATRIX_ACCESS_TOKEN"):
        return "MATRIX_ACCESS_TOKEN is not set (run `archi setup matrix`)"
    owner = cfg.get("MATRIX_OWNER_ID").strip()
    if owner and not is_mxid(owner):
        return f"MATRIX_OWNER_ID={owner!r} is not a Matrix user id (@name:server)"
    return None


class Door(BaseDoor):
    DOOR = DOOR
    PREFIX = "mx"
    PLACE = "room"
    PLACES = "rooms"
    COMMANDS = COMMANDS
    CMD_PREFIX = "!"
    HELP_TAIL = ("Send text, images, files or voice messages. Commands start with ! (Element keeps / for its own "
                 "commands; /new works too where the client sends it) and answer right away, even while a task "
                 "runs.")

    def __init__(self, api, cfg: Config | None = None, registry: Registry | None = None, *, runner=None,
                 log=None, debounce_s: float = 0.8, album_s: float = 1.5, clock=time.time, live_opts=None):
        super().__init__(cfg, registry, runner=runner,
                         log=log or (lambda *a: log_.info(" ".join(str(x) for x in a))), debounce_s=debounce_s)
        self.api = api
        self.album_s = album_s
        self.clock = clock
        self.live_opts = dict(live_opts or {})
        self.user_id = self.cfg.get("MATRIX_USER_ID").strip()
        self.since: str | None = None
        self._left: set[str] = set()          # rooms this process left: late "join" entries do not revive them
        self._encrypted_done: set[str] = set()
        self.background: set[asyncio.Task] = set()   # waits that end with the process (join and leave retries)
        self.join_retry_s: tuple[float, ...] = JOIN_RETRY_S

    @property
    def owner(self) -> str:
        return self.cfg.get("MATRIX_OWNER_ID").strip()

    @property
    def state_path(self) -> Path:
        return self.home / "state" / "matrix.json"

    def _background(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        task.add_done_callback(self._task_failed)
        return task

    # ================================================================== startup and sync
    async def run(self) -> None:
        """The door's life: startup, then the sync loop with the jobs and outbox loops until it is cancelled."""
        await self.startup()
        loops = [asyncio.create_task(self.jobs_loop()), asyncio.create_task(self.outbox_loop())]
        try:
            await self.sync_loop()
        finally:
            for t in loops:
                t.cancel()

    def _token_changed(self) -> bool:
        """`archi setup matrix` replaced the token while the door ran (the old one is logged out): use it."""
        new = self.cfg.get("MATRIX_ACCESS_TOKEN")
        if new and new != getattr(self.api, "token", new):
            self.api.set_token(new)
            self.log("the access token in the config changed: using the new one")
            return True
        return False

    async def _whoami(self) -> dict:
        """whoami, waiting out a network that is not up yet (a boot) instead of crashing."""
        delay = 2.0
        while True:
            try:
                return await self.api.whoami()
            except MxError as e:
                if e.fatal and not self._token_changed():
                    raise DoorFatal(f"the homeserver rejected the access token ({e}); run `archi setup matrix`")
                self.log("whoami failed:", e, f"(retrying in {delay:.0f} s)")
            except Exception as e:  # noqa: BLE001
                self.log("homeserver not reachable:", repr(e), f"(retrying in {delay:.0f} s)")
            await asyncio.sleep(delay)
            delay = min(60.0, delay * 2)

    async def startup(self) -> None:
        who = await self._whoami()
        uid = str(who.get("user_id") or "")
        if self.user_id and uid and uid != self.user_id:
            raise DoorFatal(f"the access token belongs to {uid}, not to MATRIX_USER_ID={self.user_id}; "
                            "run `archi setup matrix`")
        self.user_id = uid or self.user_id
        self.log("connected as", self.user_id, "· owner", self.owner or "NOT SET")
        if not self.owner:
            self.log("WARNING: MATRIX_OWNER_ID is not set; nobody can use the Matrix door (archi setup matrix)")
        self.since = self.load_since()
        if self.since is None:
            self.log("first start: the rooms' history is skipped, only new messages are answered")
        await self._report_interrupted()
        await self.flush_outbox()               # answers a previous process could not deliver

    def load_since(self) -> str | None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("user_id") != self.user_id:
            return None          # another bot account's token means nothing to this one
        return str(data.get("since") or "") or None

    def save_since(self, token: str) -> None:
        try:
            atomic_write(self.state_path, json.dumps({"since": token, "user_id": self.user_id,
                                                      "saved_at": int(time.time())}) + "\n", mode=0o600)
        except OSError as e:
            self.log("sync token not saved", repr(e))

    async def sync_once(self) -> None:
        first = self.since is None
        sync = await self.api.sync(self.since, 0 if first else SYNC_TIMEOUT_MS, SYNC_FILTER)
        await self.handle_sync(sync, initial=first)
        token = str(sync.get("next_batch") or "")
        if token:
            self.since = token
            self.save_since(token)

    async def sync_loop(self) -> None:
        delay = 2.0
        failing = False
        while True:
            try:
                await self.sync_once()
                delay = 2.0
                if failing:                     # the homeserver answers again: deliver what waited for it
                    failing = False
                    self.spawn(self.flush_outbox())
            except asyncio.CancelledError:
                raise
            except MxError as e:
                if e.fatal:
                    if self._token_changed():
                        continue
                    raise DoorFatal(f"the homeserver rejected the access token ({e}); run `archi setup matrix`")
                failing = True
                wait = (e.retry_after_ms or 0) / 1000 or delay
                self.log("sync failed:", e, f"(retrying in {wait:.0f} s)")
                await asyncio.sleep(wait)
                delay = min(60.0, delay * 2)
            except Exception as e:  # noqa: BLE001 - anything else: wait and try again
                failing = True
                self.log("sync failed:", repr(e), f"(retrying in {delay:.0f} s)")
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 2)

    async def handle_sync(self, sync: dict, *, initial: bool = False) -> None:
        rooms = sync.get("rooms") or {}
        for room_id, info in (rooms.get("invite") or {}).items():
            await self._guard(self.on_invite(room_id, info or {}))
        for room_id, info in (rooms.get("join") or {}).items():
            await self._guard(self.on_joined(room_id, info or {}, initial=initial))
        for room_id, info in (rooms.get("leave") or {}).items():
            await self._guard(self.on_left(room_id, info or {}))

    async def _guard(self, coro) -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001 - one bad room or event must never stop the door
            self.log("event handling failed", repr(e))
            log_.exception("event handling failed")

    # ================================================================== rooms and their members
    def _ensure(self, room_id: str, name: str | None = None, members: int | None = None) -> Conversation:
        """The conversation of a room the bot is in: created, revived or retitled as needed."""
        key = make_key("mx", room_id)
        conv = self.registry.get(key)
        kind = None if members is None else ("dm" if members <= 2 else "group")
        if conv is None:
            fields = {"state": "open", "kind": kind or "dm"}
            if name is not None:
                fields.update(title=name, title_confirmed=True)
            conv = self.registry.upsert(key, **fields)
            self.log("room registered", key, repr(conv.title))
            return conv
        changes: dict = {}
        if conv.state == "deleted":
            changes.update(state="open", deleted_at=0.0)
            self.log("room revived", key)
        if name is not None and (name != conv.title or not conv.title_confirmed):
            changes.update(title=name, title_confirmed=True)
        if kind and kind != conv.kind:
            changes["kind"] = kind
        return self.registry.upsert(key, **changes) if changes else conv

    def _members(self, room_id: str) -> dict | None:
        """{user: "join" | "invite"} of a room the door has seen whole, None while it has not."""
        meta = self.registry.chat(room_id)
        return dict(meta.get("members") or {}) if meta.get("members_known") else None

    def owner_in(self, room_id: str) -> bool | None:
        """Is the owner joined (or invited) in this room? None when the door does not know yet."""
        members = self._members(room_id)
        return None if members is None else members.get(self.owner) in IN_ROOM

    def private(self, room_id: str) -> bool:
        """Only the owner and the bot are in the room (nobody else joined or invited)."""
        members = self._members(room_id)
        return members is not None and not (set(members) - {self.owner, self.user_id})

    def can_post(self, conv: Conversation) -> bool:
        """Nothing is posted into a room the owner is known not to be in."""
        return conv.door != "mx" or self.owner_in(str(conv.chat_id)) is not False

    @staticmethod
    def _apply_members(members: dict, events: list) -> dict:
        for ev in events:
            if ev.get("type") == "m.room.member" and ev.get("state_key"):
                user, membership = str(ev["state_key"]), str((ev.get("content") or {}).get("membership") or "")
                if membership in IN_ROOM:
                    members[user] = membership
                else:
                    members.pop(user, None)
        return members

    async def _update_members(self, room_id: str, events: list, *, initial: bool) -> dict | None:
        """The room's members after this sync. A room the door has not seen whole yet (a first start, a fresh
        join, an upgrade from a version that kept no members) is asked for its members once."""
        known = self._members(room_id)
        if known is None:
            try:
                full = {u: m for u, m in (await self.api.members(room_id)).items() if m in IN_ROOM}
            except MxError as e:
                self.log("members of", room_id, "unknown for now:", e)
                if not initial:
                    return None
                full = self._apply_members({}, events)     # the first sync carries the whole state
            members = full
        else:
            members = self._apply_members(known, events)
        members.pop(self.user_id, None)
        self.registry.update_chat(room_id, door="mx", members=members, members_known=True)
        return members

    async def on_invite(self, room_id: str, info: dict) -> None:
        events = list(((info.get("invite_state") or {}).get("events")) or [])
        mine = [e for e in events if e.get("type") == "m.room.member" and e.get("state_key") == self.user_id]
        inviter = str((mine[-1] if mine else {}).get("sender") or "")
        name = _state_value(events, "m.room.name", "name")
        if not self.owner or inviter != self.owner:
            self.log("invite from", inviter or "?", "rejected: only the owner's invites are accepted")
            await self._leave(room_id)
            return
        encrypted = any(is_state(e, "m.room.encryption") for e in events)
        joined = await self._join(room_id)
        if joined:
            await self._joined_by_invite(room_id, name, encrypted)
        elif joined is None:
            # A pending invite is not sent again by later syncs: a join that failed for a passing reason
            # (network, a server error, rate limits) is retried in the background instead of being lost.
            self._background(self._join_later(room_id, name, encrypted))

    async def _join(self, room_id: str) -> bool | None:
        """True joined, None failed for a passing reason (worth retrying), False refused for good."""
        try:
            await self.api.join(room_id)
            return True
        except MxError as e:
            self.log("could not join", room_id, e)
            return None if e.transient else False
        except Exception as e:  # noqa: BLE001 - anything unexpected: try again later
            self.log("could not join", room_id, repr(e))
            return None

    async def _join_later(self, room_id: str, name: str | None, encrypted: bool) -> None:
        for delay in self.join_retry_s:
            await asyncio.sleep(delay)
            ok = await self._join(room_id)
            if ok:
                await self._joined_by_invite(room_id, name, encrypted)
                return
            if ok is False:
                return
        self.log("gave up joining", room_id, "after", len(self.join_retry_s), "retries; the owner can invite again")

    async def _joined_by_invite(self, room_id: str, name: str | None, encrypted: bool) -> None:
        # A new invite starts over: an earlier "this room is encrypted, I left" or "I left" belongs to the past.
        self._left.discard(room_id)
        self._encrypted_done.discard(room_id)
        self.registry.update_chat(room_id, door="mx", membership="join", name=name or "", encrypted=encrypted)
        if encrypted:
            await self._encrypted(room_id, name or "")
            return
        conv = self._ensure(room_id, name)
        self.log("joined", room_id, repr(conv.title))
        await self.send_text(conv.key, GREETING, notice=True)

    async def on_joined(self, room_id: str, info: dict, *, initial: bool) -> None:
        state = list(((info.get("state") or {}).get("events")) or [])
        timeline = list(((info.get("timeline") or {}).get("events")) or [])
        if room_id in self._left or room_id in self._encrypted_done:
            return
        if any(is_state(e, "m.room.encryption") for e in state + timeline):
            await self._encrypted(room_id, _state_value(state + timeline, "m.room.name", "name") or "")
            return
        members = await self._update_members(room_id, state + timeline, initial=initial)
        owner_events = [e for e in state + timeline if e.get("type") == "m.room.member" and self.owner
                        and e.get("state_key") == self.owner]
        final = (owner_events[-1].get("content") or {}).get("membership") if owner_events else None
        if self.owner and members is not None and members.get(self.owner) not in IN_ROOM and (
                final in ("leave", "ban") or (initial and not owner_events)):
            # The owner left this room (a leave and a re-join inside one window keep it), or was never in it at
            # the first start: the bot leaves too. A room where the owner is merely not there yet (one the agent
            # just made, whose invite is still on its way) is kept; nothing is posted there until it arrives.
            await self._owner_left(room_id)
            return
        conv = self._ensure(room_id, _state_value(state, "m.room.name", "name"),
                            None if members is None else len(members) + 1)
        if self.registry.chat(room_id).get("membership") != "join":
            self.registry.update_chat(room_id, door="mx", membership="join", encrypted=False, name=conv.title)
        for ev in timeline:
            if room_id in self._left:
                return
            if "state_key" in ev:
                await self.on_state(room_id, ev)
            elif not initial:
                await self.on_timeline(room_id, ev)

    async def on_state(self, room_id: str, ev: dict) -> None:
        if is_state(ev, "m.room.name"):
            name = str((ev.get("content") or {}).get("name") or "")
            key = make_key("mx", room_id)
            conv = self.registry.get(key)
            if conv is not None and conv.state != "deleted" and (conv.title != name or not conv.title_confirmed):
                self.registry.upsert(key, title=name, title_confirmed=True)
                self.registry.update_chat(room_id, name=name)
                self.log("room renamed", key, repr(name))

    async def on_timeline(self, room_id: str, ev: dict) -> None:
        if ev.get("sender") != self.owner or not self.owner or ev.get("sender") == self.user_id:
            return   # everybody else is ignored, silently
        if ev.get("type") == "m.room.encrypted":
            await self._encrypted(room_id, self.registry.chat(room_id).get("name", ""))
            return
        if ev.get("type") != "m.room.message":
            return
        age = self.clock() - float(ev.get("origin_server_ts") or 0) / 1000
        if age > MAX_MESSAGE_AGE_S:
            self.log("message from", f"{age / 3600:.0f} h ago not answered (the door was down)", room_id)
            return
        await self.on_message(room_id, ev)

    async def on_left(self, room_id: str, info: dict) -> None:
        timeline = list(((info.get("timeline") or {}).get("events")) or [])
        state = list(((info.get("state") or {}).get("events")) or [])
        mine = [e for e in state + timeline
                if e.get("type") == "m.room.member" and e.get("state_key") == self.user_id]
        last = mine[-1] if mine else {}
        membership = (last.get("content") or {}).get("membership", "leave")
        by = str(last.get("sender") or "")
        reason = "banned" if membership == "ban" else ("left" if by in ("", self.user_id) else "kicked")
        await self._room_gone(room_id, reason)

    async def _room_gone(self, room_id: str, reason: str) -> None:
        key = make_key("mx", room_id)
        run = self.running.get(key)
        if run:
            run.cancel.set()
        conv = self.registry.mark_deleted(key, reason=reason)
        if conv is not None:
            self.log("room gone", key, reason, "· the conversation is archived")
        if self.registry.chat(room_id):
            self.registry.update_chat(room_id, membership="leave", left_reason=reason)

    async def _owner_left(self, room_id: str) -> None:
        if room_id in self._left:
            return
        await self._room_gone(room_id, "owner left")
        await self._leave(room_id)

    async def _leave(self, room_id: str) -> None:
        self._left.add(room_id)
        try:
            await self.api.leave(room_id)
        except MxError as e:
            self.log("could not leave", room_id, e)
            if e.transient:
                self._background(self._leave_later(room_id))

    async def _leave_later(self, room_id: str) -> None:
        for delay in self.join_retry_s:
            await asyncio.sleep(delay)
            if room_id not in self._left:
                return                          # invited back in the meantime
            try:
                await self.api.leave(room_id)
                self.log("left", room_id, "on a retry")
                return
            except MxError as e:
                if not e.transient:
                    return
        self.log("gave up leaving", room_id)

    async def _encrypted(self, room_id: str, name: str = "") -> None:
        """Never pretend to work in an encrypted room: say so once, in plain text, and leave it."""
        if room_id in self._encrypted_done:
            return
        self._encrypted_done.add(room_id)
        known = self.registry.get(make_key("mx", room_id))
        name = name or (known.title if known else "") or self.registry.chat(room_id).get("name", "")
        self.log("WARNING: room", room_id, repr(name), "is end-to-end encrypted; this door cannot read it, leaving")
        self.registry.update_chat(room_id, door="mx", encrypted=True, name=name, membership="leave",
                                  left_reason="encrypted")
        try:
            await self.retrying(lambda: self.api.send(room_id, "m.room.message",
                                                      {"msgtype": "m.text", "body": ENCRYPTED_NOTICE}),
                                what="encrypted-room notice")
        except (MxError, SendFailed) as e:
            self.log("encrypted-room notice failed", room_id, e)
        await self._room_gone(room_id, "encrypted")
        await self._leave(room_id)

    # ================================================================== owner messages
    def _note_lang(self, text: str) -> None:
        if _UKRAINIAN.search(text):
            self._owner_lang = "uk"
        elif _CYRILLIC.search(text):
            self._owner_lang = "ru"
        elif re.search(r"[A-Za-z]{3}", text):
            self._owner_lang = "en"

    async def on_message(self, room_id: str, ev: dict) -> None:
        content = ev.get("content") or {}
        if (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
            return   # an edit of an earlier message
        members = self._members(room_id)
        if members is not None and members.get(self.owner) != "join":
            members[self.owner] = "join"          # the owner wrote here, so the owner is in the room
            self.registry.update_chat(room_id, members=members)
        conv = self._ensure(room_id)
        src = ev.get("event_id")
        if content.get("msgtype") == "m.text":
            text = strip_reply_fallback(str(content.get("body") or "")).strip()
            if not text:
                return
            if voice.looks_like_openai_key(text):
                self.spawn(self._store_key(conv, src, text))
                return
            self._note_lang(text)
            cmd = _COMMAND.match(text)
            if cmd and self.run_command(conv, cmd.group(1), cmd.group(2) or ""):
                return
            self._buffer_text(conv.key, text, src=src)
            return
        att = media.attachment(content)
        if att is None:
            return
        if att.kind == "voice":
            self.spawn(self._voice_turn(conv, att, src))
        elif att.kind == "image":
            self._buffer_image(conv, att, src)
        else:
            self.spawn(self._file_turn(conv, att, src))

    async def _store_key(self, conv: Conversation, event_id: str | None, secret: str) -> None:
        deleted = False
        if event_id:
            try:
                await self.api.redact(conv.chat_id, event_id, reason="contains a secret")
                deleted = True
            except MxError as e:
                self.log("could not redact the key message", e)
        await self.store_openai_key(conv.key, secret, deleted)

    def _buffer_image(self, conv: Conversation, att: media.Attachment, src) -> None:
        """Pictures sent a moment apart become one turn (Matrix has no albums: one event per picture)."""
        bkey = f"{conv.key}|images"
        buf = self._buffers.get(bkey)
        if buf:
            buf["timer"].cancel()
        else:
            buf = self._buffers[bkey] = {"items": [], "caption": "", "src": src}
        buf["items"].append(att)
        if att.caption and not buf["caption"]:
            buf["caption"] = att.caption
        buf["timer"] = self.spawn(self._flush_images(conv.key, bkey))

    async def _flush_images(self, key: str, bkey: str) -> None:
        await asyncio.sleep(self.album_s)
        buf = self._buffers.pop(bkey, None)
        if not buf:
            return
        paths: list[str] = []
        try:
            for att in buf["items"]:
                try:
                    paths.append(str(media.temp_file(await media.fetch(self.api, att), media.image_suffix(att))))
                except (MxError, ValueError, OSError) as e:
                    self.log("image download failed", e)
            if not paths:
                await self.send_text(key, "⚠️ I could not download the image.")
                return
            n = len(paths)
            caption = buf["caption"] or ("Look at this image." if n == 1 else f"Look at these {n} images.")
            await self.turn(key, caption, images=paths, src=buf["src"])
        finally:
            for p in paths:
                Path(p).unlink(missing_ok=True)

    async def _voice_turn(self, conv: Conversation, att: media.Attachment, src) -> None:
        if not voice.available(self.cfg):
            await self.ask_for_voice_key(conv.key)
            return
        try:
            path = media.temp_file(await media.fetch(self.api, att), media.audio_suffix(att))
        except (MxError, ValueError, OSError) as e:
            await self.send_text(conv.key, f"⚠️ I could not download the voice message: {e}")
            return
        await self.voice_turn(conv.key, path, src=src)

    async def _file_turn(self, conv: Conversation, att: media.Attachment, src) -> None:
        inbox = self.home / ".inbox"
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            if att.size and shutil.disk_usage(inbox).free - att.size < MIN_FREE_BYTES:
                await self.send_text(conv.key, f"⚠️ I did not save {att.name} ({att.size // (1024 * 1024)} MB): "
                                               "the server's disk would be almost full.")
                return
            data = await media.fetch(self.api, att)
            dest = inbox_path(inbox, att.name)
            dest.write_bytes(data)
        except (MxError, ValueError, OSError) as e:
            await self.send_text(conv.key, f"⚠️ I could not download the file: {e}")
            return
        self.log("file saved", dest)
        await self.turn(conv.key, file_prompt(att.kind, dest, att.name, att.caption), src=src)

    # ================================================================== turns
    def system_prompt(self, conv: Conversation, provider: str | None = None) -> str:
        where = f"the Matrix room «{conv.title}»" if conv.title else "an unnamed Matrix room"
        mx = (
            f"This conversation is {where} (room id {conv.chat_id}). Every Matrix room is a separate conversation "
            "with its own session and history; you do not see the other rooms. The owner writes to you from "
            "Element or another Matrix client.\n"
            "Matrix tools, run them from your shell (the current room is already in the environment as "
            "ARCHI_ROOM_ID):\n"
            "  archi mx room create \"<name>\"   create a new room and invite the owner (a new, separate "
            "conversation)\n"
            "  archi mx room rename \"<name>\"   rename the current room (works in rooms you created)\n"
            "  archi mx rooms                  list the known rooms\n"
            "  archi mx send \"<text>\"          post a message here now; --room <id> for another room, --notice "
            "for a quiet one\n"
            "When the owner asks for a new room, create it with the command above instead of explaining how."
        )
        return f"{base_system_prompt(self.cfg, conv.key, provider or self._provider(conv))}\n\n{mx}"

    def turn_env(self, conv: Conversation) -> dict:
        path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        code_dir = str(Path(__file__).resolve().parents[2])
        return {
            "ARCHI_HOME": str(self.home),
            "ARCHI_DOOR": DOOR,
            "ARCHI_CONVERSATION": conv.key,
            "ARCHI_ROOM_ID": str(conv.chat_id),
            "ARCHI_TOPIC_TITLE": conv.display_title(),
            "PATH": path if code_dir in path.split(":") else f"{code_dir}:{path}",
        }

    def make_live(self, conv: Conversation, st: TurnStatus, src=None) -> LiveStatus:
        return LiveStatus(self.api, str(conv.chat_id), src, st, log=self.log, **self.live_opts)

    async def job_fallback(self, conv: Conversation) -> str | None:
        """A finished job whose room is gone (or no longer the owner's) reports only in a private room of the
        owner and the bot: the most recently used one where the owner is joined, else a room this door made for
        the owner earlier, else a new unencrypted one with the owner invited. Never anywhere else."""
        best = None
        for c in self.registry.all(door="mx"):
            room = str(c.chat_id)
            if c.state == "deleted" or not self.private(room):
                continue
            meta = self.registry.chat(room)
            joined = (self._members(room) or {}).get(self.owner) == "join"
            if not joined and not (meta.get("created_by_agent") and self.owner_in(room)):
                continue
            rank = (joined, c.last_turn_at, c.updated_at)
            if best is None or rank > best[0]:
                best = (rank, c.key)
        if best is not None:
            return best[1]
        return await self._create_room_for_owner(REPORT_ROOM.format(name=self.cfg.agent_name()))

    async def _create_room_for_owner(self, name: str) -> str | None:
        if not self.owner:
            return None
        try:
            room_id = await self.api.create_room(name, [self.owner])
            if await self.api.state_event(room_id, "m.room.encryption") is not None:
                self.log("the server switched encryption on for the new room", room_id, "· leaving it")
                await self._leave(room_id)
                return None
        except MxError as e:
            self.log("could not create a room for the owner", e)
            return None
        key = make_key("mx", room_id)
        self.registry.upsert(key, title=name, title_confirmed=True, kind="dm", state="open")
        self.registry.update_chat(room_id, door="mx", membership="join", name=name, encrypted=False,
                                  created_by_agent=True, members={self.owner: "invite"}, members_known=True)
        self.log("room made for the owner", key, repr(name))
        return key

    async def delivered_late(self, key: str, record: dict) -> None:
        """The answer that waited in the outbox has arrived: its ⚠️ on the owner's message becomes ✅."""
        mark = record.get("mark") or {}
        src = mark.get("src") or record.get("src")
        if not src:
            return
        room = room_of(key)
        if mark.get("reaction"):
            try:
                await self.api.redact(room, mark["reaction"])
            except MxError as e:
                self.log("old status reaction not removed", e)
        try:
            await self.api.send(room, "m.reaction", {"m.relates_to": {"rel_type": "m.annotation", "event_id": src,
                                                                      "key": "✅"}})
        except MxError as e:
            self.log("late ✅ not set", e)

    # ================================================================== sending
    def is_transient(self, err: BaseException) -> bool:
        if not isinstance(err, MxError):
            return False
        if err.fatal:
            return self._token_changed()     # setup replaced the token: the next try uses the new one
        return err.transient

    def retry_after(self, err: BaseException) -> float | None:
        ms = getattr(err, "retry_after_ms", None)
        return ms / 1000 if ms else None

    async def _send_failed(self, key: str, err: MxError) -> bool:
        """Handle a send refused for good. False = the bot is not in that room any more, stop sending there."""
        self.log("send failed", key, err)
        if err.not_in_room:
            await self._room_gone(room_of(key), "not in the room")
            return False
        # Forbidden without saying why (some servers answer "Event is not authorized" both for a room the bot
        # left and for missing power): the rest of this answer would fail the same way. A real leave arrives
        # through the sync and archives the room there.
        return err.errcode != "M_FORBIDDEN"

    def _postable(self, key: str) -> bool:
        conv = self.registry.get(key)
        if conv is None:
            return self.owner_in(room_of(key)) is not False
        if conv.state == "deleted":
            return False
        if not self.can_post(conv):
            self.log("not posting into", key, ": the owner is not in that room")
            return False
        return True

    async def _send_event(self, room: str, content: dict, what: str) -> None:
        txn = txn_id()          # one transaction id for every try: the server stores the message once
        await self.retrying(lambda: self.api.send(room, "m.room.message", content, txn=txn), what=what)

    async def send_text(self, key: str, md: str, *, notice: bool = False, strict: bool = False) -> bool:
        """Markdown -> messages in the conversation's room. False if the bot cannot or must not post there. A
        message that still fails for a passing reason after the retries raises SendFailed with the pieces not
        sent (strict: a turn's answer, kept for later) or is logged (a command's reply)."""
        if not self._postable(key):
            return False
        pieces = render.render_messages(md)
        for i, (body, formatted) in enumerate(pieces):
            try:
                await self._send_event(room_of(key), render.message_content(body, formatted, notice=notice),
                                       "message")
            except SendFailed as e:
                if strict:
                    raise SendFailed(e.reason, remaining=[b for b, _f in pieces[i:]]) from None
                self.log("message not delivered", key, e.reason)
                return True
            except MxError as e:
                if not await self._send_failed(key, e):
                    return False
        return True

    async def send_voice(self, key: str, ogg: Path) -> bool:
        if not self._postable(key):
            return False
        data = ogg.read_bytes()
        duration = int((await voice.duration_s(str(ogg))) * 1000)
        try:
            mxc = await self.retrying(lambda: self.api.upload(data, "audio/ogg", "voice.ogg"), what="voice upload")
            content = media.voice_content("voice.ogg", len(data), duration)
            await self._send_event(room_of(key), {**content, "url": mxc}, "voice message")
        except MxError as e:
            return await self._send_failed(key, e)
        return True

    async def send_file(self, key: str, path: str) -> bool:
        if not self._postable(key):
            return False
        p = Path(path)
        if not p.is_file():
            return await self.send_text(key, f"⚠️ File not found: {path}", strict=True)
        size = p.stat().st_size
        limit = await self.api.upload_limit()
        if limit and size > limit:
            mb = 1024 * 1024
            return await self.send_text(key, f"⚠️ {p.name} is {size / mb:.1f} MB; this Matrix server accepts at "
                                             f"most {limit / mb:.0f} MB per file.", strict=True)
        data = await asyncio.to_thread(p.read_bytes)
        content, mime = media.file_content(p.name, data)
        try:
            mxc = await self.retrying(lambda: self.api.upload(data, mime, p.name), what="file upload")
            await self._send_event(room_of(key), {**content, "url": mxc}, "file")
        except MxError as e:
            if not await self._send_failed(key, e):
                return False
            return await self.send_text(key, f"⚠️ Could not send {p.name}: {e}", strict=True)
        return True


# ====================================================================== entry point

def _api(cfg: Config):
    from .api import MxApi
    return MxApi(cfg.get("MATRIX_HOMESERVER"), cfg.get("MATRIX_USER_ID"), cfg.get("MATRIX_ACCESS_TOKEN"),
                 cfg.get("MATRIX_DEVICE_ID"))


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("nio").setLevel(logging.WARNING)
    cfg = Config()
    problem = config_problem(cfg)
    if problem:
        raise SystemExit(problem)
    api = _api(cfg)
    door = Door(api, cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    fatal: BaseException | None = None
    life = asyncio.create_task(door.run())
    stopper = asyncio.create_task(stop.wait())
    try:
        log_.info("starting; home=%s", door.home)
        done, _ = await asyncio.wait({life, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if life in done and not life.cancelled():
            fatal = life.exception()       # DoorFatal: the token was rejected, or another account's token
    finally:
        # SIGTERM (systemctl stop/restart): stop the running turns so their CLI process trees are killed by the
        # engine, give them a moment, then close the HTTP session. Their run markers stay, so the next start
        # tells those rooms that the task was interrupted; an answer caught in the middle of its delivery is
        # kept in the outbox.
        door.stopping = True
        stopper.cancel()
        life.cancel()
        for t in list(door.background):
            t.cancel()
        for t in list(door.running.values()):
            t.cancel.set()
        pending = [t for t in [*door.tasks, life] if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=10)
        await api.close()
        if fatal is not None:
            raise SystemExit(f"matrix door stopped: {fatal}")


def check() -> int:
    """Dry start for `archi validate`: config and door construction (matrix-nio importable), no network."""
    cfg = Config()
    problem = config_problem(cfg)
    if problem:
        print(problem)
        return 1
    Door(_api(cfg), cfg)
    print(f"door ok: home={cfg.home}, {cfg.get('MATRIX_USER_ID')} on {cfg.get('MATRIX_HOMESERVER')}, "
          f"owner {cfg.get('MATRIX_OWNER_ID') or 'not set'}")
    return 0


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if "--check" in sys.argv[1:]:
        sys.exit(check())
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
