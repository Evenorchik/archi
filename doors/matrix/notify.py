"""Post a message into a Matrix room from outside the door process (stdlib only).

    from doors.matrix import notify
    notify.post(cfg, "mx:!room:example.org", "🧠 Session review: 2 lessons saved", silent=True)

Used by background work that has no door of its own, the learning loop's review notices for one. `silent` sends
an m.notice: Matrix's default push rules do not notify for notices, so it is read without a ping. Never raises:
a failed post returns False, and so does a post into a room the owner is not in (or an archived one).
"""
from __future__ import annotations

from core.config import Config
from core.conversations import MATRIX, Registry, parse_key

from . import render
from .api import SyncApi
from .cli import owner_absent


def post(cfg: Config, key: str, text: str, *, silent: bool = True) -> bool:
    """Send Markdown `text` to the room of conversation `key` (`mx:<room_id>`). True when every part was sent."""
    try:
        door, room_id, _thread = parse_key(key or "")
        hs, token = cfg.get("MATRIX_HOMESERVER"), cfg.get("MATRIX_ACCESS_TOKEN")
        if door != MATRIX or not room_id or not hs or not token or not (text or "").strip():
            return False
        if owner_absent(Registry(cfg.home / "state"), cfg, room_id):
            return False            # never post into a room the owner is not in
        api = SyncApi(hs, token, timeout=20)
        for content in render.contents(text, notice=silent):
            api.send(room_id, content)
        return True
    except Exception:  # noqa: BLE001 - a notice must never break the caller
        return False
