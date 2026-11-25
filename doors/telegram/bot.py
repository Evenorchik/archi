"""The Telegram door: long polling (aiogram 3), one conversation per topic, turns through core.engine.

Run: `python -m doors.telegram` (reads config/archi.env under ARCHI_HOME).

Updates are converted back to the plain JSON the Bot API documents and handled by `Door.handle_update`, so
the door does not depend on aiogram knowing the newest fields (topics in private chats, drafts, the
`stopped_message_generation` update) and its logic is testable with plain dicts. What every door does the same
way (turns, delivery, the shared commands, the learning and jobs contracts) is core.door.BaseDoor; this module
adds Telegram: pairing, topics, drafts, albums, and sending.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import sys
import time
from pathlib import Path

from core import voice
from core.config import Config
from core.conversations import Conversation, Registry, make_key, parse_key
from core.door import (KEY_REQUEST, KEY_REQUEST_EN, BaseDoor, RunningTurn, SendFailed,  # noqa: F401
                       command_list, parse_model_args)
from core.inbox import file_prompt
from core.prompt import base_system_prompt
from core.status import TurnStatus

from . import media, render
from .api import TgError
from .live import LiveStatus
from .topics import Topics, is_service, title_from_text

log_ = logging.getLogger("archi.telegram")

COMMANDS = command_list("topic", extra=[("topics", "Known topics of this chat")])
ALLOWED_UPDATES = ["message", "my_chat_member", "stopped_message_generation"]
BOT_SETTINGS = dict(has_topics_enabled=True, allows_users_to_create_topics=True, has_topic_sidebar=True,
                    can_read_all_group_messages=True, can_join_groups=True)
DOOR = "telegram"
DOWNTIME_UNCONFIRM_S = 20 * 3600   # Telegram keeps undelivered updates for 24 h: a rename during a longer
                                   # outage is lost, so stored titles can no longer be trusted for probing
HEARTBEAT_S = 300
PAIR_MAX_FAILURES = 20          # wrong codes before the pairing code is revoked (brute-force guard)
FIRST_RUN_PROMPT = (
    "[Message from the Archi door, not written by the owner] The owner has just paired with you in this "
    "private chat{lang}. This is your first conversation: follow the \"First run\" section of CLAUDE.md now. "
    "Find out this server's hostname and public IP, run `archi status` for the provider and model you run "
    "on, then introduce yourself in the owner's language: those facts, what you can do in two lines, and "
    "the two questions (what name you should have; how to work with the owner and what to do first). "
    "Keep it short; no tables."
)
REPAIRED_PROMPT = (
    "[Message from the Archi door, not written by the owner] The owner has just paired with you again in "
    "this private chat{lang}. Greet them in a few lines: your name, this server's hostname, the provider "
    "and model you run on (`archi status`), and ask what to do."
)


class Door(BaseDoor):
    DOOR = DOOR
    PREFIX = "tg"
    PLACE = "topic"
    PLACES = "topics"
    COMMANDS = COMMANDS

    def __init__(self, api, cfg: Config | None = None, registry: Registry | None = None, *, runner=None,
                 log=None, debounce_s: float = 0.8, album_s: float = 1.5):
        super().__init__(cfg, registry, runner=runner,
                         log=log or (lambda *a: log_.info(" ".join(str(x) for x in a))), debounce_s=debounce_s)
        self.api = api
        self.topics = Topics(api, self.registry, self.notify_main, log=self.log)
        self.album_s = album_s
        self.me: dict = {}
        self._pair_failures = 0
        self._pair_failures_code = ""   # the code the failures were counted against

    # ================================================================== startup
    async def startup(self) -> None:
        self.me = await self.api.call("getMe") or {}
        self.log("connected as", "@" + str(self.me.get("username")))
        try:
            await self.api.call("setMySettings", **BOT_SETTINGS)
        except TgError as e:
            self.log("setMySettings failed (topics in private chats may be off):", e.description)
        try:
            await self.api.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except TgError as e:
            self.log("setMyCommands failed:", e.description)
        if self.cfg.owner_id() is None:
            if self.cfg.get("PAIRING_CODE"):
                self.log("no owner yet: send the pairing code to the bot in a private chat")
            else:
                self.log("WARNING: neither OWNER_ID nor PAIRING_CODE is set; nobody can use this bot")
        self.check_downtime()
        self.heartbeat()
        await self._report_interrupted()
        await self.flush_outbox()               # answers a previous process could not deliver

    # ================================================================== dispatch
    async def handle_update(self, upd: dict) -> None:
        try:
            if "message" in upd:
                await self.on_message(upd["message"])
            elif "my_chat_member" in upd:
                self.topics.on_my_chat_member(upd["my_chat_member"], self.cfg.owner_id())
            elif "stopped_message_generation" in upd:
                await self.on_stopped(upd["stopped_message_generation"])
        except Exception as e:  # noqa: BLE001 - one bad update must never stop the door
            self.log("update failed", repr(e))
            log_.exception("update failed")

    def _trusted_chat(self, chat: dict, owner: int | None) -> bool:
        if owner is None:
            return False
        if chat.get("type") == "private":
            return chat.get("id") == owner
        meta = self.registry.chat(chat.get("id"))
        return bool(meta.get("trusted") or meta.get("owner_seen"))

    async def on_message(self, msg: dict) -> None:
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        owner = self.cfg.owner_id()
        if owner is None:
            if chat.get("type") == "private" and not sender.get("is_bot"):
                await self._try_pair(msg)
            return
        if is_service(msg):
            # Topic lifecycle is bookkeeping, not an answer: accept it in chats the owner uses, from anyone
            # (including the bot itself: it gets the service message for topics it created).
            if self._trusted_chat(chat, owner) or sender.get("id") == owner:
                await self.topics.on_service(msg)
            return
        if sender.get("id") != owner or sender.get("is_bot"):
            return   # everybody else is ignored, silently
        if chat.get("type") not in ("private", "group", "supergroup"):
            return
        if chat.get("type") != "private" and not self.registry.chat(chat["id"]).get("owner_seen"):
            self.registry.update_chat(chat["id"], owner_seen=True, title=chat.get("title", ""),
                                      is_forum=bool(chat.get("is_forum")))
        if sender.get("language_code"):
            self._owner_lang = sender["language_code"]
        conv = self.topics.learn(msg)
        text = (msg.get("text") or "").strip()

        if text and voice.looks_like_openai_key(text):
            self.spawn(self._store_key(conv, msg, text))
            return
        if text.startswith("/") and await self._command(conv, msg, text):
            return
        if text:
            self._maybe_auto_title(conv, text)
            self._buffer_text(conv.key, text)
            return
        att = media.attachment_of(msg)
        if att is None:
            return
        kind, obj = att
        if kind == "voice":
            self.spawn(self._voice_turn(conv, msg, obj))
            return
        if kind == "photo":
            self._buffer_photo(conv, msg, obj)
            return
        self.spawn(self._file_turn(conv, msg, kind, obj))

    # ================================================================== pairing and keys
    async def _try_pair(self, msg: dict) -> None:
        code = self.cfg.get("PAIRING_CODE").strip()
        text = (msg.get("text") or "").strip()
        if text.lower().startswith("/start"):
            text = text[6:].strip()
        if code != self._pair_failures_code:
            # A new code (`archi pair`) starts a new count: failures against an old, revoked code must not
            # make the first typo with the new one revoke it again.
            self._pair_failures, self._pair_failures_code = 0, code
        if not code or not text or not hmac.compare_digest(text.encode(), code.encode()):
            if code and re.fullmatch(r"\d{4,10}", text):
                self._pair_failures += 1
                if self._pair_failures >= PAIR_MAX_FAILURES:
                    self.cfg.set("PAIRING_CODE", "")
                    self.log(f"{PAIR_MAX_FAILURES} wrong pairing codes: the code is revoked; "
                             "make a new one with `archi pair`")
            return   # silence: a stranger learns nothing, not even that a code exists
        uid = msg["from"]["id"]
        self.cfg.update({"OWNER_ID": str(uid), "PAIRING_CODE": ""})
        self._pair_failures = 0
        self.log("paired with owner", uid)
        lang = (msg["from"].get("language_code") or "").strip()
        if lang:
            self._owner_lang = lang
        try:
            await self._send_plain(msg["chat"]["id"], msg.get("message_thread_id") if msg.get("is_topic_message")
                                   else None, "✅ Paired. You are my owner now; I answer only you.")
        except TgError as e:   # the greeting below matters more than this line
            self.log("pairing confirmation not sent", e.description)
        # The first turn is the door's, not the owner's: the agent introduces itself and asks for its name
        # ("First run" in CLAUDE.md), so a fresh install starts a conversation instead of waiting in silence.
        conv = self.topics.learn(msg)
        hint = f" (their Telegram language code: {lang})" if lang else ""
        prompt = (FIRST_RUN_PROMPT if not self.cfg.has_name() else REPAIRED_PROMPT).format(lang=hint)
        self.spawn(self.turn(conv.key, prompt))

    async def _store_key(self, conv: Conversation, msg: dict, key: str) -> None:
        try:
            await self.api.call("deleteMessage", chat_id=msg["chat"]["id"], message_id=msg["message_id"])
            deleted = True
        except TgError:
            deleted = False
        await self.store_openai_key(conv.key, key, deleted)

    # ================================================================== commands
    async def _command(self, conv: Conversation, msg: dict, text: str) -> bool:
        head, _, args = text[1:].partition(" ")
        name, _, target = head.partition("@")
        if target and self.me.get("username") and target.lower() != str(self.me["username"]).lower():
            return True   # addressed to another bot in a group
        return self.run_command(conv, name, args)

    def command_handlers(self) -> dict:
        return {**super().command_handlers(), "topics": self._cmd_topics, "start": self._cmd_help}

    async def _cmd_topics(self, conv: Conversation, args: str) -> None:
        convs = self.registry.all(door="tg", chat_id=conv.chat_id, include_deleted=args == "all")
        lines = [f"**Topics in this chat** ({len(convs)})"]
        now = time.time()
        for c in convs:
            ago = _ago(now - c.last_turn_at) if c.last_turn_at else "no turns yet"
            mark = "▶ " if c.key in self.running else ""
            where = "main" if c.is_main else f"#{c.thread_id}"
            lines.append(f"· {mark}{c.display_title()} ({where}) · {c.state} · {self._provider(c)} · {ago}")
        lines.append("\nCreate a topic: ask me, or create it in Telegram; every topic is its own conversation.")
        await self.send_text(conv.key, "\n".join(lines))

    async def on_stopped(self, upd: dict) -> None:
        chat = upd.get("chat") or {}
        owner = self.cfg.owner_id()
        if chat.get("id") != owner:
            return
        key = make_key("tg", chat["id"], upd.get("message_thread_id"))
        run = self.running.get(key)
        if run is None:
            return
        if run.live and upd.get("draft_id") not in (None, run.live.draft_id):
            self.log("stop for an old draft ignored", key)
            return
        self.log("stop pressed", key)
        run.cancel.set()

    # ================================================================== buffering
    def _buffer_photo(self, conv: Conversation, msg: dict, obj: dict) -> None:
        gid = msg.get("media_group_id") or f"single-{msg.get('message_id')}"
        bkey = f"{conv.key}|photo|{gid}"
        buf = self._buffers.get(bkey)
        if buf:
            buf["timer"].cancel()
        else:
            buf = self._buffers[bkey] = {"photos": [], "caption": ""}
        buf["photos"].append(obj)
        if msg.get("caption") and not buf["caption"]:
            buf["caption"] = msg["caption"]
        buf["timer"] = self.spawn(self._flush_photos(conv.key, bkey))

    async def _flush_photos(self, key: str, bkey: str) -> None:
        await asyncio.sleep(self.album_s)
        buf = self._buffers.pop(bkey, None)
        if not buf:
            return
        paths = []
        for obj in buf["photos"]:
            try:
                paths.append(str(await media.save_temp(self.api, obj, ".jpg")))
            except TgError as e:
                self.log("photo download failed", e.description)
        if not paths:
            await self.send_text(key, "⚠️ I could not download the photo.")
            return
        n = len(paths)
        caption = buf["caption"] or ("Look at this image." if n == 1 else f"Look at these {n} images.")
        try:
            await self.turn(key, caption, images=paths)
        finally:
            for p in paths:
                Path(p).unlink(missing_ok=True)

    # ================================================================== media turns
    async def _voice_turn(self, conv: Conversation, msg: dict, obj: dict) -> None:
        if not voice.available(self.cfg):
            await self.ask_for_voice_key(conv.key)
            return
        try:
            path = await media.save_temp(self.api, obj, ".ogg")
        except TgError as e:
            await self.send_text(conv.key, f"⚠️ I could not download the voice note: {e.description}")
            return
        await self.voice_turn(conv.key, path)

    async def _file_turn(self, conv: Conversation, msg: dict, kind: str, obj: dict) -> None:
        try:
            dest = await media.save_to_inbox(self.api, kind, obj, self.home / ".inbox")
        except TgError as e:
            await self.send_text(conv.key, f"⚠️ I could not download the file: {e.description}")
            return
        caption = msg.get("caption") or ""
        await self.turn(conv.key, file_prompt(kind, dest, obj.get("file_name") or dest.name, caption))

    def _maybe_auto_title(self, conv: Conversation, text: str) -> None:
        """Topics the owner opened without a name ("New Chat") get one from their first message."""
        if (conv.is_main or conv.kind != "dm" or not conv.name_implicit or conv.last_turn_at
                or self.cfg.get("TG_AUTO_TITLE") in ("0", "false", "no")):
            return
        title = title_from_text(text)
        if title:
            self.spawn(self.topics.rename(conv, title))

    # ================================================================== turns
    def system_prompt(self, conv: Conversation, chat_title: str | None = None, provider: str | None = None) -> str:
        if chat_title is None:
            chat_title = self.registry.chat(conv.chat_id).get("title", "")
        if conv.is_main:
            where = ("the main part of the private chat, outside any topic" if conv.kind == "dm"
                     else f"the General topic of the group «{chat_title or conv.chat_id}»")
        else:
            where = (f"the topic «{conv.display_title()}» in the private chat" if conv.kind == "dm"
                     else f"the topic «{conv.display_title()}» in the group «{chat_title or conv.chat_id}»")
        tg = (
            f"This conversation is {where}. Every Telegram topic is a separate conversation with its own "
            "session and history; you do not see the other topics.\n"
            "Telegram tools, run them from your shell (the current chat and topic are already in the "
            "environment as ARCHI_CHAT_ID and ARCHI_THREAD_ID):\n"
            "  archi tg topic create \"<name>\"   create a new topic in this chat (a new, separate conversation)\n"
            "  archi tg topic rename \"<name>\"   rename the current topic\n"
            "  archi tg topics                  list the known topics of this chat\n"
            "  archi tg send \"<text>\"           post a message here now; --thread <id|main> for another topic\n"
            "When the owner asks for a new topic, create it with the command above instead of explaining how."
        )
        return f"{base_system_prompt(self.cfg, conv.key, provider or self._provider(conv))}\n\n{tg}"

    def turn_env(self, conv: Conversation) -> dict:
        path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        code_dir = str(Path(__file__).resolve().parents[2])
        return {
            "ARCHI_HOME": str(self.home),
            "ARCHI_DOOR": "telegram",
            "ARCHI_CHAT_ID": str(conv.chat_id),
            "ARCHI_THREAD_ID": "" if conv.is_main else str(conv.thread_id),
            "ARCHI_CONVERSATION": conv.key,
            "ARCHI_TOPIC_TITLE": conv.display_title(),
            "PATH": path if code_dir in path.split(":") else f"{code_dir}:{path}",
        }

    def make_live(self, conv: Conversation, st: TurnStatus, src=None) -> LiveStatus:
        return LiveStatus(self.api, conv.chat_id, conv.thread_id, conv.kind, st, log=self.log)

    async def job_fallback(self, conv: Conversation) -> str | None:
        """A deleted topic reports a finished job in the main part of the same chat."""
        key = make_key("tg", conv.chat_id, None)
        self.registry.ensure(key, kind=conv.kind, state="open", title="main")
        return key

    def is_transient(self, err: BaseException) -> bool:
        """No network (code 0), flood control (429) or a Telegram server error: worth another try."""
        return isinstance(err, TgError) and (err.code == 0 or err.code == 429 or err.code >= 500)

    def retry_after(self, err: BaseException) -> float | None:
        return getattr(err, "retry_after", None)

    # ================================================================== downtime
    @property
    def alive_path(self) -> Path:
        return self.home / "state" / "telegram.alive"

    def heartbeat(self) -> None:
        try:
            self.alive_path.parent.mkdir(parents=True, exist_ok=True)
            self.alive_path.write_text(str(int(time.time())))
        except OSError as e:
            self.log("heartbeat not written", repr(e))

    def check_downtime(self, now: float | None = None) -> float | None:
        """Called once at startup, before the first heartbeat. After an outage longer than Telegram keeps
        updates, a topic may have been renamed without us hearing about it: probing it with the stored
        name would rename it back. Such titles stop counting as confirmed until a new rename event (or a
        rename by the agent) confirms them again. Returns the outage length, None on the first start."""
        now = time.time() if now is None else now
        try:
            last = self.alive_path.stat().st_mtime
        except OSError:
            return None
        down = now - last
        if down > DOWNTIME_UNCONFIRM_S:
            n = 0
            for conv in self.registry.all(door="tg"):
                if conv.title_confirmed and not conv.is_main:
                    self.registry.upsert(conv.key, title_confirmed=False)
                    n += 1
            self.log(f"door was down {down / 3600:.0f} h: {n} topic title(s) no longer trusted for probing")
        return down

    async def heartbeat_loop(self, interval: float = HEARTBEAT_S) -> None:
        while True:
            self.heartbeat()
            await asyncio.sleep(interval)

    # ================================================================== sending
    async def _send_failed(self, key: str, err: TgError) -> bool:
        """Handle a failed send to a conversation. False = the topic is gone, stop sending there."""
        if err.topic_gone and parse_key(key)[2] is not None:
            await self.topics.gone(key, "send error")
            return False
        self.log("send failed", key, err.description)
        return True

    async def send_voice(self, key: str, ogg: Path) -> bool:
        _door, chat, thread = parse_key(key)
        try:
            await self.retrying(lambda: media.send_voice(self.api, int(chat), thread, ogg), what="voice note")
        except TgError as e:
            return await self._send_failed(key, e)
        return True

    async def send_file(self, key: str, path: str) -> bool:
        _door, chat, thread = parse_key(key)
        try:
            problem = await self.retrying(lambda: media.send_file(self.api, int(chat), thread, path), what="file")
        except TgError as e:
            if not await self._send_failed(key, e):
                return False
            problem = f"⚠️ Could not send {path}: {e.description}"
        if problem:
            return await self.send_text(key, problem, strict=True)
        return True

    async def send_text(self, key: str, md: str, *, strict: bool = False) -> bool:
        """Markdown -> Telegram messages in the conversation's topic. False if the topic is gone. A message that
        still fails for a passing reason after the retries raises SendFailed with the pieces not sent (strict:
        a turn's answer, kept for later) or is logged (a command's reply)."""
        _door, chat, thread = parse_key(key)
        pieces = render.split_markdown(md, limit=render.LIMIT - 196)
        for i, piece in enumerate(pieces):
            try:
                for part in render.render_messages(piece):
                    if not await self._send_part(key, int(chat), thread, part):
                        return False
            except SendFailed as e:
                if strict:
                    raise SendFailed(e.reason, remaining=pieces[i:]) from None
                self.log("message not delivered", key, e.reason)
                return True
        return True

    async def _send_part(self, key: str, chat: int, thread: int | None, part: str) -> bool:
        try:
            await self.retrying(lambda: self.api.call("sendMessage", chat_id=chat, message_thread_id=thread,
                                                      text=part, parse_mode="HTML",
                                                      link_preview_options={"is_disabled": True}))
            return True
        except TgError as e:
            if e.bad_markup:
                try:
                    await self.retrying(lambda: self.api.call("sendMessage", chat_id=chat, message_thread_id=thread,
                                                              text=render.strip_html(part)))
                    return True
                except TgError as e2:
                    e = e2
            return await self._send_failed(key, e)

    async def _send_plain(self, chat_id: int, thread: int | None, text: str) -> None:
        await self.api.call("sendMessage", chat_id=chat_id, message_thread_id=thread, text=text)

    async def notify_main(self, chat_id: int, text: str) -> None:
        await self.api.call("sendMessage", chat_id=chat_id, text=text)


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


# ====================================================================== entry point

async def amain() -> None:
    from aiogram import Bot, Dispatcher

    from .api import TgApi
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(f"TELEGRAM_BOT_TOKEN is not set in {cfg.path}")
    bot = Bot(token=token)
    api = TgApi(token, bot=bot)
    door = Door(api, cfg)
    await door.startup()
    dp = Dispatcher()

    async def outer(handler, update, data):
        raw = update.model_dump(mode="json", exclude_unset=True, by_alias=True)
        await door.handle_update(raw)
        return None   # the door handled it; aiogram's routers are not used

    dp.update.outer_middleware(outer)
    interval = max(60, cfg.get_int("TG_PROBE_INTERVAL", 900))
    probe = asyncio.create_task(door.topics.probe_loop(interval))
    loops = [asyncio.create_task(door.heartbeat_loop()), asyncio.create_task(door.jobs_loop()),
             asyncio.create_task(door.outbox_loop())]
    log_.info("polling; home=%s", door.home)
    try:
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES, handle_as_tasks=False)
    finally:
        # SIGTERM (systemctl stop/restart): stop the running turns so their CLI process trees are killed
        # by the engine, give them a moment, then close the HTTP sessions. Their run markers stay, so the
        # next start tells those conversations that the task was interrupted.
        door.stopping = True
        probe.cancel()
        for t in loops:
            t.cancel()
        for t in list(door.running.values()):
            t.cancel.set()
        pending = [t for t in door.tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=10)
        await api.close()


def check() -> int:
    """Dry start for `archi validate`: config, token format and door construction, no network calls."""
    cfg = Config()
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(f"TELEGRAM_BOT_TOKEN is not set in {cfg.path}")
        return 1
    from aiogram import Bot
    from aiogram.utils.token import TokenValidationError

    from .api import TgApi
    try:
        bot = Bot(token=token)
    except TokenValidationError:
        print("TELEGRAM_BOT_TOKEN has an invalid format")
        return 1
    Door(TgApi(token, bot=bot), cfg)
    print(f"door ok: home={cfg.home}, provider={cfg.provider()}")
    return 0


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if "--check" in sys.argv[1:]:
        sys.exit(check())
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
