"""The Telegram door: long polling (aiogram 3), one conversation per topic, turns through core.engine.

Run: `python -m doors.telegram` (reads config/archi.env under ARCHI_HOME).

Updates are converted back to the plain JSON the Bot API documents and handled by `Door.handle_update`, so
the door does not depend on aiogram knowing the newest fields (topics in private chats, drafts, the
`stopped_message_generation` update) and its logic is testable with plain dicts.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from core import engine, voice
from core.config import Config, atomic_write, home
from core.conversations import Conversation, Registry, make_key, parse_key
from core.markers import split_answer
from core.prompt import base_system_prompt, stamp
from core.status import TurnStatus, clock

from . import media, render
from .api import TgError
from .live import LiveStatus
from .topics import Topics, is_service, key_of, thread_of, title_from_text

log_ = logging.getLogger("archi.telegram")

COMMANDS = [
    ("new", "Start a new session in this topic"),
    ("status", "Model, session and what is running"),
    ("model", "Show or change the model for this topic"),
    ("provider", "Show or switch the agent: claude or codex"),
    ("topics", "Known topics of this chat"),
    ("stop", "Stop the task running in this topic"),
    ("help", "What I can do here"),
]
ALLOWED_UPDATES = ["message", "my_chat_member", "stopped_message_generation"]
BOT_SETTINGS = dict(has_topics_enabled=True, allows_users_to_create_topics=True, has_topic_sidebar=True,
                    can_read_all_group_messages=True, can_join_groups=True)
KEY_REQUEST = {
    "ru": "Дай мне ключ, я внесу его в конфиг",
    "uk": "Дай мне ключ, я внесу его в конфиг",
}
KEY_REQUEST_EN = "Send me an OpenAI API key and I will add it to the config"


@dataclass
class RunningTurn:
    key: str
    cancel: asyncio.Event
    status: TurnStatus
    live: LiveStatus | None = None
    started: float = field(default_factory=time.time)


class Door:
    def __init__(self, api, cfg: Config | None = None, registry: Registry | None = None, *, runner=None,
                 log=None, debounce_s: float = 0.8, album_s: float = 1.5):
        self.api = api
        self.cfg = cfg or Config()
        self.home = self.cfg.home
        self.registry = registry or Registry(self.home / "state")
        self.runner = runner or engine.run_turn
        self.log = log or (lambda *a: log_.info(" ".join(str(x) for x in a)))
        self.topics = Topics(api, self.registry, self.notify_main, log=self.log)
        self.debounce_s = debounce_s
        self.album_s = album_s
        self.me: dict = {}
        self.running: dict[str, RunningTurn] = {}
        self.tasks: set[asyncio.Task] = set()
        self.started = time.time()
        self._buffers: dict[str, dict] = {}
        self._owner_lang = ""
        self.inflight_path = self.home / "state" / "inflight.json"

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
        await self._report_interrupted()

    async def _report_interrupted(self) -> None:
        """Turns that were running when the door stopped were killed with it; say so where they ran."""
        stale = self._inflight_read()
        if not stale:
            return
        self._inflight_write({})
        for key, info in stale.items():
            _door, chat, thread = parse_key(key)
            try:
                await self.send_text(key, "⚠️ I was restarted while a task was running here, so it was "
                                          "interrupted. Send the request again if it is still needed.")
            except Exception as e:  # noqa: BLE001
                self.log("restart notice failed", key, repr(e))

    def _inflight_read(self) -> dict:
        try:
            data = json.loads(self.inflight_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _inflight_write(self, data: dict) -> None:
        try:
            atomic_write(self.inflight_path, json.dumps(data))
        except OSError as e:
            self.log("inflight write failed", repr(e))

    def _inflight(self, key: str, on: bool) -> None:
        data = self._inflight_read()
        if on:
            data[key] = {"started_at": time.time()}
        else:
            data.pop(key, None)
        self._inflight_write(data)

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

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

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
        if not code or not text or not hmac.compare_digest(text.encode(), code.encode()):
            return   # silence: a stranger learns nothing, not even that a code exists
        uid = msg["from"]["id"]
        self.cfg.update({"OWNER_ID": str(uid), "PAIRING_CODE": ""})
        self.log("paired with owner", uid)
        name = self.cfg.agent_name()
        await self._send_plain(msg["chat"]["id"], None,
                               f"✅ Paired. You are my owner now; I answer only you.\n\nI am {name}. Write to me "
                               "here, or create topics: every topic is a separate conversation.")

    def _lang(self) -> str:
        return (getattr(self, "_owner_lang", "") or "").split("-")[0].lower()

    async def _store_key(self, conv: Conversation, msg: dict, key: str) -> None:
        try:
            await self.api.call("deleteMessage", chat_id=msg["chat"]["id"], message_id=msg["message_id"])
            deleted = True
        except TgError:
            deleted = False
        verdict = await voice.check_key(key)
        if verdict is False:
            await self.send_text(conv.key, "⚠️ OpenAI rejected this key, so I did not save it.")
            return
        self.cfg.set("OPENAI_API_KEY", key)
        self.log("OPENAI_API_KEY stored", "verified" if verdict else "not verified")
        if self._lang() in ("ru", "uk"):
            text = "✅ Ключ сохранён в конфиг. Пришли голосовое ещё раз."
            if not verdict:
                text += " (Проверить ключ сейчас не получилось, он будет опробован на следующем голосовом.)"
            if not deleted:
                text += " Удали своё сообщение с ключом: у меня не вышло."
        else:
            text = "✅ Key saved to the config. Please send the voice note again."
            if not verdict:
                text += " (I could not verify it right now; it will be tried on the next voice note.)"
            if not deleted:
                text += " Please delete your message with the key, I could not do it."
        await self.send_text(conv.key, text)

    # ================================================================== commands
    async def _command(self, conv: Conversation, msg: dict, text: str) -> bool:
        head, _, args = text[1:].partition(" ")
        name, _, target = head.partition("@")
        if target and self.me.get("username") and target.lower() != str(self.me["username"]).lower():
            return True   # addressed to another bot in a group
        name = name.lower()
        handler = {"new": self._cmd_new, "status": self._cmd_status, "model": self._cmd_model,
                   "provider": self._cmd_provider, "topics": self._cmd_topics, "stop": self._cmd_stop,
                   "help": self._cmd_help, "start": self._cmd_help}.get(name)
        if handler is None:
            return False   # unknown slash text is a normal message (the engine neutralises the slash)
        self.spawn(handler(conv, args.strip()))   # commands do not wait for the running turn
        return True

    def _provider(self, conv: Conversation) -> str:
        return conv.provider if conv.provider in engine.PROVIDERS else self.cfg.provider()

    def _model(self, conv: Conversation, provider: str) -> tuple[str, str]:
        """(model, effort) for a turn: the topic's override, else the config default of its provider.
        The override always belongs to the topic's current provider (switching providers clears it)."""
        if provider == "codex":
            return conv.model or self.cfg.get("CODEX_MODEL"), ""
        effort = conv.effort or self.cfg.get("CLAUDE_EFFORT")
        return conv.model or self.cfg.get("CLAUDE_MODEL"), (effort if effort in engine.EFFORTS else "")

    async def _cmd_new(self, conv: Conversation, args: str) -> None:
        had = self.registry.clear_session(conv.key)
        busy = conv.key in self.running
        text = "🧹 New session for this topic: I start from a clean slate here. Other topics are not affected."
        if not had:
            text = "🧹 There was no session in this topic yet, so nothing was lost. The next message starts one."
        if busy:
            text += "\nA task is still running here; its result will not continue in the new session."
        await self.send_text(conv.key, text)

    async def _cmd_status(self, conv: Conversation, args: str) -> None:
        conv = self.registry.get(conv.key) or conv
        provider = self._provider(conv)
        model, effort = self._model(conv, provider)
        sid = conv.sessions.get(provider)
        run = self.running.get(conv.key)
        open_convs = [c for c in self.registry.all(door="tg") if c.state != "deleted"]
        up = int(time.time() - self.started)
        lines = [
            f"**{conv.display_title()}** · {conv.state}",
            f"· agent: `{provider}` · model: `{model or 'CLI default'}`"
            + (f" · effort: {effort or 'default'}" if provider == "claude" else ""),
            f"· session: `{sid[:8]}…`" if sid else "· session: none yet (the next message starts one)",
        ]
        if run:
            lines.append(f"· running: {clock(run.status.elapsed)}, {len(run.status.steps)} steps"
                         + (f", now: {run.status.current}" if run.status.current else ""))
        else:
            lines.append("· running: nothing")
        lines += [f"· conversations: {len(open_convs)}, running now: {len(self.running)}",
                  f"· voice notes: {'on' if voice.available(self.cfg) else 'off (no OpenAI key)'}",
                  f"· up for {up // 3600} h {up % 3600 // 60} min"]
        await self.send_text(conv.key, "\n".join(lines))

    async def _cmd_model(self, conv: Conversation, args: str) -> None:
        conv = self.registry.get(conv.key) or conv
        provider = self._provider(conv)
        model, effort = self._model(conv, provider)
        if not args:
            await self.send_text(conv.key, "\n".join([
                f"Model in this topic: `{model or 'CLI default'}`"
                + (f" · effort: {effort or 'default'}" if provider == "claude" else "") + f" ({provider})",
                "",
                "· `/model sonnet` or `/model claude-sonnet-4-5`: another model for this topic",
                "· `/model opus high`: model and reasoning effort; `/model high`: effort only",
                "· `/model default`: back to the default from the config",
                f"· efforts: {', '.join(engine.EFFORTS)}",
            ]))
            return
        new_model, new_effort, err = parse_model_args(args.split(), provider)
        if err:
            await self.send_text(conv.key, f"⚠️ {err}")
            return
        if new_model == "default":
            self.registry.upsert(conv.key, model="", effort="")
            conv = self.registry.get(conv.key)
            m, e = self._model(conv, provider)
            await self.send_text(conv.key, f"✅ Back to the default: `{m or 'CLI default'}`")
            return
        cand_model = new_model or model
        cand_effort = new_effort if new_effort is not None else ("" if new_model else effort)
        ok, why = await engine.probe_model(provider, cand_model, cand_effort, cwd=self.home)
        if not ok:
            await self.send_text(conv.key, f"⚠️ Not switched: `{cand_model}` did not answer a test request ({why}).")
            return
        self.registry.upsert(conv.key, model=new_model or conv.model, effort=cand_effort or "")
        await self.send_text(conv.key, f"✅ This topic now uses `{cand_model or 'CLI default'}`"
                                       + (f" with effort {cand_effort}" if cand_effort else "")
                                       + ". Checked with a test request.")

    async def _cmd_provider(self, conv: Conversation, args: str) -> None:
        conv = self.registry.get(conv.key) or conv
        current = self._provider(conv)
        want = args.lower().strip()
        if not want:
            await self.send_text(conv.key, f"Agent in this topic: `{current}`. Switch: `/provider claude` or "
                                           "`/provider codex`.")
            return
        if want not in engine.PROVIDERS:
            await self.send_text(conv.key, "Usage: `/provider claude` or `/provider codex`")
            return
        binary = engine.resolve_binary(want)
        if not (os.path.isabs(binary) and os.path.exists(binary)) and not shutil.which(binary):
            await self.send_text(conv.key, f"⚠️ The `{want}` CLI is not installed on this server.")
            return
        changes = {"provider": want}
        if want != current:
            changes.update(model="", effort="")   # a model override never carries over to the other agent
        self.registry.upsert(conv.key, **changes)
        await self.send_text(conv.key, f"✅ This topic now talks to `{want}`. Sessions are kept per agent, "
                                       f"so an earlier {want} session here continues.")

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

    async def _cmd_stop(self, conv: Conversation, args: str) -> None:
        run = self.running.get(conv.key)
        if not run:
            await self.send_text(conv.key, "Nothing is running in this topic.")
            return
        run.cancel.set()

    async def _cmd_help(self, conv: Conversation, args: str) -> None:
        name = self.cfg.agent_name()
        await self.send_text(conv.key, "\n".join([
            f"**{name}**: every topic is a separate conversation, topics run in parallel.",
            "",
            *[f"· /{c} — {d}" for c, d in COMMANDS],
            "",
            "Send text, photos, files or voice notes. Commands answer right away, even while a task runs.",
        ]))

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
    def _buffer_text(self, key: str, text: str) -> None:
        """Telegram splits a long paste into several messages a moment apart: merge them into one turn."""
        buf = self._buffers.get(key)
        if buf and buf.get("timer"):
            buf["timer"].cancel()
        else:
            buf = self._buffers[key] = {"parts": []}
        buf["parts"].append(text)
        buf["timer"] = self.spawn(self._flush_text(key))

    async def _flush_text(self, key: str) -> None:
        await asyncio.sleep(self.debounce_s)
        buf = self._buffers.pop(key, None)
        if buf:
            self.spawn(self.turn(key, "\n".join(buf["parts"])))

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
            await self.send_text(conv.key, KEY_REQUEST.get(self._lang(), KEY_REQUEST_EN))
            return
        try:
            path = await media.save_temp(self.api, obj, ".ogg")
        except TgError as e:
            await self.send_text(conv.key, f"⚠️ I could not download the voice note: {e.description}")
            return
        try:
            text = await voice.transcribe(str(path), self.cfg)
        except Exception as e:  # noqa: BLE001
            await self.send_text(conv.key, f"⚠️ Transcription failed: {e}")
            return
        finally:
            path.unlink(missing_ok=True)
        if not text:
            await self.send_text(conv.key, "⚠️ I could not make out any speech in that voice note.")
            return
        await self.turn(conv.key, f"[Voice message, transcribed automatically]\n{text}")

    async def _file_turn(self, conv: Conversation, msg: dict, kind: str, obj: dict) -> None:
        try:
            dest = await media.save_to_inbox(self.api, kind, obj, self.home / ".inbox")
        except TgError as e:
            await self.send_text(conv.key, f"⚠️ I could not download the file: {e.description}")
            return
        caption = (msg.get("caption") or "").strip()
        prompt = (f"The owner sent a file ({kind}); it is saved at: {dest}\n"
                  f"Original name: {obj.get('file_name') or dest.name}\n"
                  "Do NOT execute it automatically: read it first.\n"
                  + (f"The owner's instruction with the file:\n{caption}" if caption else
                     "There was no instruction: say what the file is and what you propose to do, then wait."))
        await self.turn(conv.key, prompt)

    def _maybe_auto_title(self, conv: Conversation, text: str) -> None:
        """Topics the owner opened without a name ("New Chat") get one from their first message."""
        if (conv.is_main or conv.kind != "dm" or not conv.name_implicit or conv.last_turn_at
                or self.cfg.get("TG_AUTO_TITLE") in ("0", "false", "no")):
            return
        title = title_from_text(text)
        if title:
            self.spawn(self.topics.rename(conv, title))

    # ================================================================== turns
    def system_prompt(self, conv: Conversation, chat_title: str = "") -> str:
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
        return f"{base_system_prompt(self.cfg)}\n\n{tg}"

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

    async def turn(self, key: str, prompt: str, images: list[str] | None = None) -> None:
        """Run one turn of conversation `key` (queued behind a running one) and deliver the answer."""
        async with self.registry.lock(key):
            conv = self.registry.get(key)
            if conv is None or conv.state == "deleted":
                return
            provider = self._provider(conv)
            model, effort = self._model(conv, provider)
            gen = self.registry.generation(key)
            st = TurnStatus()
            live = LiveStatus(self.api, conv.chat_id, conv.thread_id, conv.kind, st, log=self.log)
            run = RunningTurn(key, asyncio.Event(), st, live)
            self.running[key] = run
            self._inflight(key, True)
            chat_title = self.registry.chat(conv.chat_id).get("title", "")

            def on_event(ev: engine.Event) -> None:
                st.feed(ev)
                live.poke()

            await live.start()
            res = None
            try:
                res = await self.runner(provider, f"{stamp(self.cfg)}\n{prompt}", conv.sessions.get(provider),
                                        cwd=self.home, images=images, on_event=on_event,
                                        env=self.turn_env(conv), system_prompt=self.system_prompt(conv, chat_title),
                                        model=model, effort=effort, cancel=run.cancel)
            except Exception as e:  # noqa: BLE001
                res = engine.TurnResult("", None, False, f"internal error: {e!r}")
            finally:
                self.running.pop(key, None)
                self._inflight(key, False)
                st.finish(res or engine.TurnResult("", None, False, "interrupted"))
                await live.stop(bool(res and res.ok))
            try:
                if res.session_id:
                    self.registry.set_session(key, provider, res.session_id, gen=gen)
                self.registry.upsert(key, last_turn_at=time.time())
            except Exception as e:  # noqa: BLE001 - bookkeeping must never eat the answer
                self.log("session save failed", key, repr(e))
            await self.deliver(key, res, st)

    async def deliver(self, key: str, res: engine.TurnResult, st: TurnStatus) -> None:
        if res.cancelled:
            await self.send_text(key, f"⏹ Stopped after {clock(st.elapsed)}.")
            return
        text = res.answer or ""
        if not res.ok:
            text = (text + "\n\n" if text else "") + f"⚠️ {res.error or 'the agent did not answer'}"
        clean, files, voices = split_answer(text)
        if clean and not await self.send_text(key, clean):
            return
        _door, chat, thread = parse_key(key)
        for lang, speech, speed in voices:
            ogg = await voice.synthesize(speech, self.cfg, speed=speed) if voice.available(self.cfg) else None
            if ogg is None:
                await self.send_text(key, speech)
                continue
            try:
                await media.send_voice(self.api, int(chat), thread, ogg)
            except TgError as e:
                if not await self._send_failed(key, e):
                    return
            finally:
                ogg.unlink(missing_ok=True)
        for path in files:
            try:
                problem = await media.send_file(self.api, int(chat), thread, path)
            except TgError as e:
                if not await self._send_failed(key, e):
                    return
                problem = f"⚠️ Could not send {path}: {e.description}"
            if problem:
                await self.send_text(key, problem)

    # ================================================================== sending
    async def _send_failed(self, key: str, err: TgError) -> bool:
        """Handle a failed send to a conversation. False = the topic is gone, stop sending there."""
        if err.topic_gone and parse_key(key)[2] is not None:
            await self.topics.gone(key, "send error")
            return False
        self.log("send failed", key, err.description)
        return True

    async def send_text(self, key: str, md: str) -> bool:
        """Markdown -> Telegram messages in the conversation's topic. False if the topic is gone."""
        _door, chat, thread = parse_key(key)
        for part in render.render_messages(md):
            try:
                await self.api.call("sendMessage", chat_id=int(chat), message_thread_id=thread, text=part,
                                    parse_mode="HTML", link_preview_options={"is_disabled": True})
            except TgError as e:
                if e.bad_markup:
                    try:
                        await self.api.call("sendMessage", chat_id=int(chat), message_thread_id=thread,
                                            text=render.strip_html(part))
                        continue
                    except TgError as e2:
                        e = e2
                if not await self._send_failed(key, e):
                    return False
        return True

    async def _send_plain(self, chat_id: int, thread: int | None, text: str) -> None:
        await self.api.call("sendMessage", chat_id=chat_id, message_thread_id=thread, text=text)

    async def notify_main(self, chat_id: int, text: str) -> None:
        await self.api.call("sendMessage", chat_id=chat_id, text=text)


def parse_model_args(args: list[str], provider: str) -> tuple[str | None, str | None, str | None]:
    """`/model` arguments -> (model | None, effort | "" | None, error | None).
    model "default" = drop the override; effort None = not mentioned, "" = back to the default."""
    model = effort = None
    for raw in args:
        w = raw.strip()
        low = w.lower()
        if low in engine.EFFORTS:
            if provider != "claude":
                return None, None, "reasoning effort can only be set for claude"
            effort = low
        elif low in ("default", "reset"):
            if model is None and effort is None and len(args) == 1:
                return "default", None, None
            effort = ""
        elif model is None:
            if provider == "claude" and not low.startswith("claude-") and low not in ("opus", "sonnet", "haiku",
                                                                                         "fable"):
                low = "claude-" + low.replace(".", "-").replace("_", "-")
            model = low if provider == "claude" else w
        else:
            return None, None, f"I did not understand «{raw}»; efforts are {', '.join(engine.EFFORTS)}"
    return model, effort, None


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
    log_.info("polling; home=%s", door.home)
    try:
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES, handle_as_tasks=False)
    finally:
        probe.cancel()
        for t in list(door.running.values()):
            t.cancel.set()
        await api.close()


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
