"""Door-independent parts of what every turn is told: who the agent is, the clock line, answer markers."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config


def _tz(cfg: Config):
    name = cfg.get("TIMEZONE")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo


def stamp(cfg: Config, now: datetime | None = None) -> str:
    """One-line clock prefix for every incoming message. A one-shot CLI turn has no clock of its own:
    "today" or "tomorrow" in the owner's message would be unanchored without it."""
    tz = _tz(cfg)
    now = (now or datetime.now(tz)).astimezone(tz)
    zone = getattr(tz, "key", None) or now.strftime("%Z") or "local time"
    return f"[{now:%d.%m.%Y %H:%M}, {now:%A} · {zone}]"


def base_system_prompt(cfg: Config) -> str:
    ident = cfg.identity()
    name = cfg.agent_name()
    who = (f"You are {name}, a personal agent" if cfg.has_name() else
           "You are a personal agent without a name yet (the owner gives you one on the first run; until "
           f"then you may call yourself {name})")
    parts = [
        f"{who} that runs on its owner's own server with full access to it. "
        "The owner talks to you through a messenger; every message you get comes from the owner.",
        "The first line of each owner message is a timestamp added by the door, for example "
        "[25.11.2026 14:05, Wednesday · Europe/Berlin]. It is context, not the owner's words: use it to "
        "resolve \"today\" or \"tomorrow\", never quote it back.",
        "Answer in the owner's language. Markdown is converted for the messenger; avoid tables.",
        "To send a file, put a line `[FILE /absolute/path]` in your answer (only when the owner asks for a "
        "file). To send a voice note, end the answer with `[VOICE lang=\"en\"]text to speak[/VOICE]`, only "
        "when the owner explicitly asks for a voice reply.",
        "Files the owner sends are saved under .inbox/ in your home directory and you get their path; "
        "never execute an attached script unless the owner clearly asks for that.",
    ]
    persona = str(ident.get("persona") or "").strip()
    if persona:
        parts.append(persona[:8000])
    return "\n\n".join(parts)
