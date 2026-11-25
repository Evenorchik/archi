"""The Matrix door: Element (or any Matrix client) rooms as conversations, turns through core.engine.

Run: `python -m doors.matrix` (reads config/archi.env under ARCHI_HOME); `--check` is the dry start of
`archi validate`.

* Every room the bot is in is one conversation, key `mx:<room_id>`, with its own session; rooms run in
  parallel. The room name is the conversation's title. A room the bot is kicked from or leaves, and a room
  the owner leaves (the bot then leaves too), is archived like a deleted Telegram topic.
* Only MATRIX_OWNER_ID is answered and only the owner's invites are accepted; other invites are rejected and
  everybody else is ignored, silently.
* No end-to-end encryption (matrix-nio without its e2e extra), so an encrypted room cannot be read. The bot
  never stays in one: invited into an encrypted room, or when encryption is switched on in a room it is in, it
  says so once in plain text and leaves. `archi status` counts those rooms.
* The sync token is kept in state/matrix.json: a restart neither answers a message twice nor misses one that
  arrived while the door was down (up to MAX_MESSAGE_AGE_S old). The first start ever skips the history.
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
from core.door import BaseDoor, command_list
from core.inbox import file_prompt, inbox_path
from core.prompt import base_system_prompt
from core.status import TurnStatus

from . import media, render
from .api import MxError, is_mxid
from .live import LiveStatus

log_ = logging.getLogger("archi.matrix")

DOOR = "matrix"
COMMANDS = command_list("room")
SYNC_TIMEOUT_MS = 30000
MAX_MESSAGE_AGE_S = 24 * 3600     # an owner message older than this (the door was down) is not answered
MIN_FREE_BYTES = 512 * 1024 * 1024
SYNC_FILTER = {"presence": {"types": []}, "account_data": {"types": []},
               "room": {"timeline": {"limit": 50}, "ephemeral": {"types": []}, "account_data": {"types": []}}}
GREETING = "👋 I am here. This room is a separate conversation with its own session: write to me here."
ENCRYPTED_NOTICE = ("🔒 This room is end-to-end encrypted, and I cannot read encrypted messages, so I am leaving it. "
                    "Create a room with encryption switched off (in Element: New room, then turn off \"Enable "
                    "end-to-end encryption\") and invite me there.")
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


def _state_value(events: list, event_type: str, field: str) -> str | None:
    value = None
    for ev in events:
        if ev.get("type") == event_type and ev.get("state_key", "") == "":
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

    @property
    def owner(self) -> str:
        return self.cfg.get("MATRIX_OWNER_ID").strip()

    @property
    def state_path(self) -> Path:
        return self.home / "state" / "matrix.json"

    # ================================================================== startup and sync
    async def run(self) -> None:
        """The door's life: startup, then the sync loop and the jobs loop until it is cancelled."""
        await self.startup()
        jobs_task = asyncio.create_task(self.jobs_loop())
        try:
            await self.sync_loop()
        finally:
            jobs_task.cancel()

    async def _whoami(self) -> dict:
        """whoami, waiting out a network that is not up yet (a boot) instead of crashing."""
        delay = 2.0
        while True:
            try:
                return await self.api.whoami()
            except MxError as e:
                if e.fatal:
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
        while True:
            try:
                await self.sync_once()
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except MxError as e:
                if e.fatal:
                    raise DoorFatal(f"the homeserver rejected the access token ({e}); run `archi setup matrix`")
                wait = (e.retry_after_ms or 0) / 1000 or delay
                self.log("sync failed:", e, f"(retrying in {wait:.0f} s)")
                await asyncio.sleep(wait)
                delay = min(60.0, delay * 2)
            except Exception as e:  # noqa: BLE001 - network trouble: wait and try again
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

    # ================================================================== rooms
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

    async def on_invite(self, room_id: str, info: dict) -> None:
        events = list(((info.get("invite_state") or {}).get("events")) or [])
        mine = [e for e in events if e.get("type") == "m.room.member" and e.get("state_key") == self.user_id]
        inviter = str((mine[-1] if mine else {}).get("sender") or "")
        name = _state_value(events, "m.room.name", "name")
        if not self.owner or inviter != self.owner:
            self.log("invite from", inviter or "?", "rejected: only the owner's invites are accepted")
            await self._leave(room_id)
            return
        encrypted = any(e.get("type") == "m.room.encryption" for e in events)
        for attempt in range(2):
            try:
                await self.api.join(room_id)
                break
            except MxError as e:
                self.log("could not join", room_id, e)
                if attempt or e.fatal:
                    return
                await asyncio.sleep(2)
        self._left.discard(room_id)
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
        if any(e.get("type") == "m.room.encryption" for e in state + timeline):
            await self._encrypted(room_id, _state_value(state + timeline, "m.room.name", "name") or "")
            return
        owner_left = [e for e in state if e.get("type") == "m.room.member" and e.get("state_key") == self.owner
                      and (e.get("content") or {}).get("membership") in ("leave", "ban")]
        if self.owner and owner_left:
            await self._owner_left(room_id)          # left before this sync window (or before the first start)
            return
        members = (info.get("summary") or {}).get("m.joined_member_count")
        conv = self._ensure(room_id, _state_value(state, "m.room.name", "name"),
                            members if isinstance(members, int) else None)
        if self.registry.chat(room_id).get("membership") != "join":
            self.registry.update_chat(room_id, door="mx", membership="join", encrypted=False,
                                      name=conv.title)
        for ev in timeline:
            if room_id in self._left:
                return
            if "state_key" in ev:
                await self.on_state(room_id, ev)
            elif not initial:
                await self.on_timeline(room_id, ev)

    async def on_state(self, room_id: str, ev: dict) -> None:
        etype, content = ev.get("type"), ev.get("content") or {}
        key = make_key("mx", room_id)
        if etype == "m.room.name" and ev.get("state_key", "") == "":
            name = str(content.get("name") or "")
            conv = self.registry.get(key)
            if conv is not None and conv.state != "deleted" and (conv.title != name or not conv.title_confirmed):
                self.registry.upsert(key, title=name, title_confirmed=True)
                self.registry.update_chat(room_id, name=name)
                self.log("room renamed", key, repr(name))
        elif etype == "m.room.member" and ev.get("state_key") == self.owner and self.owner:
            if content.get("membership") in ("leave", "ban"):
                await self._owner_left(room_id)

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
            await self.api.send(room_id, "m.room.message", {"msgtype": "m.text", "body": ENCRYPTED_NOTICE})
        except MxError as e:
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
        paths = []
        for att in buf["items"]:
            try:
                paths.append(str(media.temp_file(await media.fetch(self.api, att), media.image_suffix(att))))
            except (MxError, ValueError) as e:
                self.log("image download failed", e)
        if not paths:
            await self.send_text(key, "⚠️ I could not download the image.")
            return
        n = len(paths)
        caption = buf["caption"] or ("Look at this image." if n == 1 else f"Look at these {n} images.")
        try:
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
        except (MxError, ValueError) as e:
            await self.send_text(conv.key, f"⚠️ I could not download the voice message: {e}")
            return
        await self.voice_turn(conv.key, path, src=src)

    async def _file_turn(self, conv: Conversation, att: media.Attachment, src) -> None:
        inbox = self.home / ".inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        if att.size and shutil.disk_usage(inbox).free - att.size < MIN_FREE_BYTES:
            await self.send_text(conv.key, f"⚠️ I did not save {att.name} ({att.size // (1024 * 1024)} MB): the "
                                           "server's disk would be almost full.")
            return
        try:
            data = await media.fetch(self.api, att)
        except (MxError, ValueError) as e:
            await self.send_text(conv.key, f"⚠️ I could not download the file: {e}")
            return
        dest = inbox_path(inbox, att.name)
        dest.write_bytes(data)
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

    def job_fallback(self, conv: Conversation) -> str | None:
        """A finished job whose room is gone reports in the room the owner used most recently."""
        rooms = [c for c in self.registry.all(door="mx") if c.state != "deleted"]
        if not rooms:
            return None
        return max(rooms, key=lambda c: (c.last_turn_at, c.updated_at)).key

    # ================================================================== sending
    async def _send_failed(self, key: str, err: MxError) -> bool:
        """Handle a failed send. False = the bot is not in that room any more, stop sending there."""
        self.log("send failed", key, err)
        if err.not_in_room:
            await self._room_gone(room_of(key), "not in the room")
            return False
        # Forbidden without saying why (some servers answer "Event is not authorized" both for a room the bot
        # left and for missing power): the rest of this answer would fail the same way. A real leave arrives
        # through the sync and archives the room there.
        return err.errcode != "M_FORBIDDEN"

    async def send_text(self, key: str, md: str, *, notice: bool = False) -> bool:
        """Markdown -> messages in the conversation's room. False if the bot cannot post there any more."""
        conv = self.registry.get(key)
        if conv is not None and conv.state == "deleted":
            return False
        for content in render.contents(md, notice=notice):
            try:
                await self.api.send(room_of(key), "m.room.message", content)
            except MxError as e:
                if not await self._send_failed(key, e):
                    return False
        return True

    async def send_voice(self, key: str, ogg: Path) -> bool:
        data = ogg.read_bytes()
        duration = int((await voice.duration_s(str(ogg))) * 1000)
        try:
            mxc = await self.api.upload(data, "audio/ogg", "voice.ogg")
            content = media.voice_content("voice.ogg", len(data), duration)
            await self.api.send(room_of(key), "m.room.message", {**content, "url": mxc})
        except MxError as e:
            return await self._send_failed(key, e)
        return True

    async def send_file(self, key: str, path: str) -> bool:
        p = Path(path)
        if not p.is_file():
            return await self.send_text(key, f"⚠️ File not found: {path}")
        size = p.stat().st_size
        limit = await self.api.upload_limit()
        if limit and size > limit:
            mb = 1024 * 1024
            return await self.send_text(key, f"⚠️ {p.name} is {size / mb:.1f} MB; this Matrix server accepts at "
                                             f"most {limit / mb:.0f} MB per file.")
        data = await asyncio.to_thread(p.read_bytes)
        content, mime = media.file_content(p.name, data)
        try:
            mxc = await self.api.upload(data, mime, p.name)
            await self.api.send(room_of(key), "m.room.message", {**content, "url": mxc})
        except MxError as e:
            if not await self._send_failed(key, e):
                return False
            await self.send_text(key, f"⚠️ Could not send {p.name}: {e}")
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
        # tells those rooms that the task was interrupted.
        door.stopping = True
        stopper.cancel()
        life.cancel()
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
