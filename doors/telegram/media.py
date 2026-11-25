"""Files and voice in and out of Telegram.

In: documents are saved to <home>/.inbox/<timestamp>_<name> and only their path is handed to the agent
(never executed automatically); photos and voice notes go to a temporary file. Out: `[FILE /path]` markers
become documents (pictures as photos), `[VOICE]` blocks become voice notes.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path

from .api import TgError

MAX_DOWNLOAD = 20 * 1024 * 1024        # Bot API getFile limit
MAX_UPLOAD = 50 * 1024 * 1024
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_MAX = 10 * 1024 * 1024


def safe_name(raw: str | None, default: str = "file") -> str:
    """Strip any path component and hostile characters from a user-supplied filename."""
    base = Path(raw or default).name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".") or default
    return base[:80]


def attachment_of(msg: dict) -> tuple[str, dict] | None:
    """-> (kind, object) for the first attachment in a message, or None."""
    for kind in ("document", "video", "animation", "video_note", "audio", "voice", "sticker"):
        if msg.get(kind):
            return kind, msg[kind]
    if msg.get("photo"):
        return "photo", msg["photo"][-1]   # the largest size
    return None


async def download(api, obj: dict, dest: Path) -> Path:
    if (obj.get("file_size") or 0) > MAX_DOWNLOAD:
        raise TgError(f"file is larger than {MAX_DOWNLOAD // (1024 * 1024)} MB, Telegram does not let bots "
                      "download it", 413, "getFile")
    dest.parent.mkdir(parents=True, exist_ok=True)
    return await api.download(obj["file_id"], dest)


async def save_to_inbox(api, kind: str, obj: dict, inbox: Path) -> Path:
    ext = {"video": ".mp4", "animation": ".mp4", "video_note": ".mp4", "voice": ".ogg", "audio": ".mp3",
           "photo": ".jpg", "sticker": ".webp"}.get(kind, "")
    name = safe_name(obj.get("file_name") or f"{kind}{ext}")
    dest = inbox / f"{time.strftime('%Y%m%d-%H%M%S')}_{name}"
    return await download(api, obj, dest)


async def save_temp(api, obj: dict, suffix: str) -> Path:
    fd, tmp = tempfile.mkstemp(prefix="archi_tg_", suffix=suffix)
    os.close(fd)
    return await download(api, obj, Path(tmp))


async def send_file(api, chat_id: int, thread_id: int | None, path: str) -> str | None:
    """Send one file. Returns an error text for the owner, or None on success."""
    p = Path(path)
    if not p.is_file():
        return f"⚠️ File not found: {path}"
    size = p.stat().st_size
    if size > MAX_UPLOAD:
        return f"⚠️ {p.name} is {size // (1024 * 1024)} MB; Telegram bots can send at most 50 MB."
    if p.suffix.lower() in PHOTO_SUFFIXES and size <= PHOTO_MAX:
        try:
            await api.call("sendPhoto", chat_id=chat_id, message_thread_id=thread_id, photo=p)
            return None
        except TgError as e:
            if e.topic_gone:
                raise
    await api.call("sendDocument", chat_id=chat_id, message_thread_id=thread_id, document=p)
    return None


async def send_voice(api, chat_id: int, thread_id: int | None, ogg: Path) -> None:
    await api.call("sendVoice", chat_id=chat_id, message_thread_id=thread_id, voice=ogg)
