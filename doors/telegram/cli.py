"""`archi tg ...`: Telegram actions for the agent itself, run from its shell during a turn.

The door puts the current chat and topic into ARCHI_CHAT_ID / ARCHI_THREAD_ID for every turn, so
`archi tg topic create "Name"` creates the topic in the chat the owner is talking in. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import sys

from core.config import Config
from core.conversations import Registry, make_key

from . import render
from .api import SyncApi, TgError


def _chat(args) -> int:
    raw = args.chat or os.environ.get("ARCHI_CHAT_ID", "")
    if not raw:
        raise SystemExit("no chat: pass --chat or run inside a turn (ARCHI_CHAT_ID)")
    return int(raw)


def _thread(args) -> int | None:
    raw = args.thread if args.thread is not None else os.environ.get("ARCHI_THREAD_ID", "")
    if raw in ("", "main", "0", None):
        return None
    return int(raw)


def _api(cfg: Config) -> SyncApi:
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(f"TELEGRAM_BOT_TOKEN is not set in {cfg.path}")
    return SyncApi(token)


def cmd_topic_create(args, cfg: Config, reg: Registry) -> int:
    chat = _chat(args)
    name = args.name.strip()[:128]
    if not name:
        raise SystemExit("topic name is empty")
    api = _api(cfg)
    res = api.call("createForumTopic", chat_id=chat, name=name)
    thread = int(res["message_thread_id"])
    key = make_key("tg", chat, thread)
    reg.upsert(key, title=res.get("name", name), title_confirmed=True, name_implicit=False,
               kind="dm" if chat > 0 else "group", state="open")
    print(f"created topic «{res.get('name', name)}»: thread {thread}, conversation {key}")
    if chat > 0:
        try:
            me = api.call("getMe")
            print(f"link: https://t.me/{me['username']}/{thread}")
        except (TgError, KeyError):
            pass
    return 0


def cmd_topic_rename(args, cfg: Config, reg: Registry) -> int:
    chat, thread = _chat(args), _thread(args)
    if thread is None:
        raise SystemExit("this is the main conversation, not a topic; pass --thread")
    name = args.name.strip()[:128]
    try:
        _api(cfg).call("editForumTopic", chat_id=chat, message_thread_id=thread, name=name)
    except TgError as e:
        if not e.not_modified:
            raise
    reg.upsert(make_key("tg", chat, thread), title=name, title_confirmed=True, name_implicit=False)
    print(f"renamed topic {thread} to «{name}»")
    return 0


def cmd_topics(args, cfg: Config, reg: Registry) -> int:
    chat = None if args.all else _chat(args)
    convs = reg.all(door="tg", chat_id=chat, include_deleted=args.deleted)
    if not convs:
        print("no known conversations")
    for c in convs:
        provider = c.provider or cfg.provider()
        thread = "main" if c.is_main else str(c.thread_id)
        print(f"{c.key}\t{thread}\t{c.state}\t{provider}\t{c.display_title()}")
    return 0


def cmd_send(args, cfg: Config, reg: Registry) -> int:
    chat, thread = _chat(args), _thread(args)
    api = _api(cfg)
    text = args.text if args.text != "-" else sys.stdin.read()
    parts = [render.escape(text)] if args.plain else render.render_messages(text)
    for part in parts:
        try:
            api.call("sendMessage", chat_id=chat, message_thread_id=thread, text=part, parse_mode="HTML",
                     link_preview_options={"is_disabled": True})
        except TgError as e:
            if not e.bad_markup:
                raise
            api.call("sendMessage", chat_id=chat, message_thread_id=thread, text=render.strip_html(part))
    print(f"sent {len(parts)} message(s)")
    return 0


def add_parser(sub) -> None:
    tg = sub.add_parser("tg", help="Telegram actions (topics, messages)")
    tsub = tg.add_subparsers(dest="tg_cmd", required=True)

    def common(p):
        p.add_argument("--chat", help="chat id (default: the current chat, ARCHI_CHAT_ID)")
        p.add_argument("--thread", help="topic thread id or 'main' (default: the current topic)")

    topic = tsub.add_parser("topic", help="create or rename a topic")
    topsub = topic.add_subparsers(dest="topic_cmd", required=True)
    p = topsub.add_parser("create", help="create a topic in the current chat")
    p.add_argument("name")
    p.add_argument("--chat")
    p.set_defaults(func=cmd_topic_create)
    p = topsub.add_parser("rename", help="rename the current topic")
    p.add_argument("name")
    common(p)
    p.set_defaults(func=cmd_topic_rename)

    p = tsub.add_parser("topics", help="list known topics of the current chat")
    p.add_argument("--chat")
    p.add_argument("--all", action="store_true", help="all chats")
    p.add_argument("--deleted", action="store_true", help="include deleted topics")
    p.set_defaults(func=cmd_topics)

    p = tsub.add_parser("send", help="send a message (Markdown) to the current topic")
    p.add_argument("text", help="text, or - to read it from stdin")
    p.add_argument("--plain", action="store_true", help="send as plain text")
    common(p)
    p.set_defaults(func=cmd_send)


def run(args) -> int:
    cfg = Config()
    reg = Registry(cfg.home / "state")
    try:
        return args.func(args, cfg, reg)
    except TgError as e:
        print(f"telegram error: {e.description}", file=sys.stderr)
        return 1
