"""`archi mx ...`: Matrix actions for the agent itself, run from its shell during a turn. Stdlib only.

The door puts the current room into ARCHI_ROOM_ID for every turn, so `archi mx room rename "Name"` renames the
room the owner is talking in, and `archi mx send` posts there.
"""
from __future__ import annotations

import os
import sys

from core.config import Config
from core.conversations import Registry, make_key

from . import render
from .api import MxError, SyncApi, room_link


def _api(cfg: Config) -> SyncApi:
    hs, token = cfg.get("MATRIX_HOMESERVER"), cfg.get("MATRIX_ACCESS_TOKEN")
    if not hs or not token:
        raise SystemExit("the Matrix door is not set up (run `archi setup matrix`)")
    return SyncApi(hs, token)


def _room(args) -> str:
    raw = (getattr(args, "room", None) or os.environ.get("ARCHI_ROOM_ID", "")).strip()
    if raw.startswith("mx:"):
        raw = raw[3:]
    if not raw:
        raise SystemExit("no room: pass --room or run inside a turn in a Matrix room (ARCHI_ROOM_ID)")
    return raw


def cmd_room_create(args, cfg: Config, reg: Registry) -> int:
    name = args.name.strip()[:255]
    if not name:
        raise SystemExit("the room name is empty")
    owner = cfg.get("MATRIX_OWNER_ID").strip()
    api = _api(cfg)
    room_id = api.create_room(name, [owner] if owner else [], topic=args.topic or "")
    key = make_key("mx", room_id)
    reg.upsert(key, title=name, title_confirmed=True, kind="dm", state="open")
    reg.update_chat(room_id, door="mx", membership="join", name=name, encrypted=False, created_by_agent=True)
    print(f"created room «{name}»: {room_id}, conversation {key}")
    print(f"link: {room_link(room_id)}")
    print(f"invited the owner ({owner}); the room appears in their Element as an invitation"
          if owner else "MATRIX_OWNER_ID is not set: nobody was invited")
    if api.state(room_id, "m.room.encryption") is not None:
        print("WARNING: the server switched encryption on for this room, so the door cannot read it; the room "
              "will be left. Check the server's default-encryption setting.", file=sys.stderr)
        return 1
    return 0


def cmd_room_rename(args, cfg: Config, reg: Registry) -> int:
    room_id = _room(args)
    name = args.name.strip()[:255]
    if not name:
        raise SystemExit("the room name is empty")
    try:
        _api(cfg).set_name(room_id, name)
    except MxError as e:
        if e.errcode == "M_FORBIDDEN":
            print(f"not renamed: this account may not rename the room ({e.error}). In a room the owner created "
                  "the bot needs moderator rights for that; the owner can also rename it in Element.",
                  file=sys.stderr)
            return 1
        raise
    reg.upsert(make_key("mx", room_id), title=name, title_confirmed=True)
    print(f"renamed room {room_id} to «{name}»")
    return 0


def cmd_rooms(args, cfg: Config, reg: Registry) -> int:
    convs = reg.all(door="mx", include_deleted=args.deleted)
    if not convs:
        print("no known rooms")
    current = os.environ.get("ARCHI_ROOM_ID", "")
    for c in convs:
        provider = c.provider or cfg.provider()
        here = "  (this room)" if c.chat_id == current else ""
        print(f"{c.key}\t{c.state}\t{provider}\t{c.display_title()}{here}")
    encrypted = [rid for rid, meta in reg.chats(door="mx").items() if meta.get("encrypted")]
    if encrypted:
        print(f"{len(encrypted)} encrypted room(s) were left: the door cannot read encrypted rooms")
    return 0


def cmd_send(args, cfg: Config, reg: Registry) -> int:
    room_id = _room(args)
    text = args.text if args.text != "-" else sys.stdin.read()
    if not text.strip():
        raise SystemExit("nothing to send")
    api = _api(cfg)
    if args.plain:
        contents = [{"msgtype": "m.notice" if args.notice else "m.text", "body": text}]
    else:
        contents = render.contents(text, notice=args.notice)
    for content in contents:
        api.send(room_id, content)
    print(f"sent {len(contents)} message(s)")
    return 0


def add_parser(sub) -> None:
    mx = sub.add_parser("mx", help="Matrix actions (rooms, messages)")
    msub = mx.add_subparsers(dest="mx_cmd", required=True)

    room = msub.add_parser("room", help="create or rename a room")
    rsub = room.add_subparsers(dest="room_cmd", required=True)
    p = rsub.add_parser("create", help="create an unencrypted room and invite the owner")
    p.add_argument("name")
    p.add_argument("--topic", default="")
    p.set_defaults(func=cmd_room_create)
    p = rsub.add_parser("rename", help="rename the current room (ARCHI_ROOM_ID) or --room")
    p.add_argument("name")
    p.add_argument("--room", help="room id (default: the current room, ARCHI_ROOM_ID)")
    p.set_defaults(func=cmd_room_rename)

    p = msub.add_parser("rooms", help="list the known rooms (conversations)")
    p.add_argument("--deleted", action="store_true", help="include archived rooms")
    p.set_defaults(func=cmd_rooms)

    p = msub.add_parser("send", help="send a message (Markdown) to the current room")
    p.add_argument("text", help="text, or - to read it from stdin")
    p.add_argument("--room", help="room id (default: the current room, ARCHI_ROOM_ID)")
    p.add_argument("--notice", action="store_true", help="send as a notice (no notification)")
    p.add_argument("--plain", action="store_true", help="send as plain text")
    p.set_defaults(func=cmd_send)


def run(args) -> int:
    cfg = Config()
    reg = Registry(cfg.home / "state")
    try:
        return args.func(args, cfg, reg)
    except MxError as e:
        print(f"matrix error: {e}", file=sys.stderr)
        return 1
