"""`archi identity set-name "<name>"`: give the agent its name everywhere at once.

  * config/archi.env: AGENT_NAME (and config/identity.json "name", when that file sets one);
  * CLAUDE.md: the `{{AGENT_NAME}}` placeholder, or the previous name in the identity line;
  * Telegram: the bot's display name (Bot API setMyName).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core.config import CODE_DIR, Config, atomic_write

PLACEHOLDER = "{{AGENT_NAME}}"
IDENTITY_LINE = re.compile(r"(You are \*\*)(.+?)(\*\*, the owner's personal agent)")
NAME_MAX = 64          # Bot API limit for setMyName


def clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise ValueError("the name is empty")
    if len(name) > NAME_MAX:
        raise ValueError(f"the name is longer than {NAME_MAX} characters")
    if "*" in name or "{" in name or "}" in name:
        raise ValueError("the name must not contain * { or }")
    return name


def rename_in_rulebook(text: str, name: str) -> tuple[str, bool]:
    """-> (new text, changed). Replaces the placeholder everywhere; without one, the identity line."""
    if PLACEHOLDER in text:
        return text.replace(PLACEHOLDER, name), True
    new, n = IDENTITY_LINE.subn(lambda m: f"{m.group(1)}{name}{m.group(3)}", text, count=1)
    return new, bool(n) and new != text


def set_name(raw: str, *, cfg: Config | None = None, code_dir: Path = CODE_DIR, api=None, out=print) -> int:
    try:
        name = clean_name(raw)
    except ValueError as e:
        out(f"Not changed: {e}.")
        return 2
    cfg = cfg or Config()
    cfg.set("AGENT_NAME", name)
    ident = cfg.identity()
    if ident.get("name"):
        ident["name"] = name
        atomic_write(cfg.identity_path, json.dumps(ident, ensure_ascii=False, indent=1) + "\n", mode=0o600)
    out(f"config: AGENT_NAME = {name}")

    rulebook = Path(code_dir) / "CLAUDE.md"
    if rulebook.is_symlink():
        rulebook = rulebook.resolve()
    try:
        text = rulebook.read_text(encoding="utf-8")
        new, changed = rename_in_rulebook(text, name)
        if changed:
            atomic_write(rulebook, new, mode=rulebook.stat().st_mode & 0o777)
            out(f"CLAUDE.md: the identity now names {name}")
        else:
            out("CLAUDE.md: no placeholder or identity line found, left as it is")
    except OSError as e:
        out(f"CLAUDE.md not updated: {e}")

    token = cfg.get("TELEGRAM_BOT_TOKEN")
    if not token:
        out("Telegram: no bot token configured, bot name not changed")
        return 0
    if api is None:
        from doors.telegram.api import SyncApi
        api = SyncApi(token)
    from doors.telegram.api import TgError
    try:
        api.call("setMyName", name=name)
        out(f"Telegram: the bot is now called {name}")
    except TgError as e:
        hint = f" (Telegram asks to wait {int(e.retry_after)} s)" if e.retry_after else ""
        out(f"Telegram: the bot name was not changed: {e.description}{hint}. "
            "Run the same command again later; config and CLAUDE.md are already updated.")
        return 1
    return 0
