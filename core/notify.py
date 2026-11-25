"""Post a short message into a conversation from outside its door: learning-review notices, curator reports.

    post("tg:<chat>:<thread|main>", "🧠 **Session review** …", silent=True, home=home) -> bool

Dispatch by the conversation key's prefix: `tg:` goes straight to the Telegram Bot API (the stdlib client
and the door's own Markdown renderer; `disable_notification` for silent posts); `mx:` goes to the Matrix
door's notifier, `doors.matrix.notify.post(cfg, key, text, silent=…)`, imported lazily because that door is
optional. Whatever goes wrong, the result is False and a log line, never an exception: a notice must not
break the work that produced it.
"""
from __future__ import annotations

import logging
import os

from .config import Config
from .conversations import parse_key

log = logging.getLogger("archi.notify")


def post(key: str, text: str, *, silent: bool = True, home: str | os.PathLike | None = None) -> bool:
    try:
        if not text or not text.strip():
            return False
        cfg = Config(home) if home else Config()
        if key.startswith("tg:"):
            return _telegram(cfg, key, text, silent)
        if key.startswith("mx:"):
            return _matrix(cfg, key, text, silent)
        log.warning("notify: no door for conversation %r", key)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("notify to %s failed: %r", key, e)
        return False


def _telegram(cfg: Config, key: str, text: str, silent: bool) -> bool:
    from doors.telegram import render
    from doors.telegram.api import SyncApi, TgError

    token = cfg.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("notify: TELEGRAM_BOT_TOKEN is not set")
        return False
    _door, chat, thread = parse_key(key)
    api = SyncApi(token, timeout=20)
    for part in render.render_messages(text):
        params = dict(chat_id=int(chat), message_thread_id=thread, text=part, parse_mode="HTML",
                      disable_notification=bool(silent), link_preview_options={"is_disabled": True})
        try:
            api.call("sendMessage", **params)
        except TgError as e:
            if not e.bad_markup:
                log.warning("notify to %s failed: %s", key, e.description)
                return False
            params.pop("parse_mode")
            params["text"] = render.strip_html(part)
            try:
                api.call("sendMessage", **params)
            except TgError as e2:
                log.warning("notify to %s failed: %s", key, e2.description)
                return False
    return True


def _matrix(cfg: Config, key: str, text: str, silent: bool) -> bool:
    try:
        from doors.matrix import notify as mx_notify
    except Exception as e:  # noqa: BLE001 - the Matrix door is optional; any import problem means "no door"
        log.warning("notify: the Matrix door is not available (%r)", e)
        return False
    return bool(mx_notify.post(cfg, key, text, silent=silent))
