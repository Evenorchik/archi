"""Where the agent's long-term memory lives, for every part of Archi that reads or writes it.

The memory store is a directory of small Markdown files (one fact per file) with an index `MEMORY.md`. It is
the memory directory Claude Code itself uses for the Archi home, so Claude turns load the index on their
own; Codex turns get the index in their system prompt (see core/prompt.py). Override: ARCHI_MEMORY_DIR.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

INDEX = "MEMORY.md"
PERSONA_MAX = 8000          # characters of a conversation's own rules put into the system prompt
INDEX_MAX = 12000           # characters of the memory index put into a Codex system prompt


def claude_slug(path: str | os.PathLike) -> str:
    """The key Claude Code derives from a working directory: every non-alphanumeric character becomes '-'
    (/opt/archi -> -opt-archi). Its transcripts and memory live under ~/.claude/projects/<slug>/."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def claude_project_dir(home: str | os.PathLike) -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "projects" / claude_slug(home)


def memory_dir(home: str | os.PathLike) -> Path:
    env = os.environ.get("ARCHI_MEMORY_DIR")
    return Path(env) if env else claude_project_dir(home) / "memory"


def index_text(home: str | os.PathLike, limit: int = INDEX_MAX) -> str:
    """The memory index, cut to `limit` characters at a line boundary; "" when there is none."""
    try:
        text = (memory_dir(home) / INDEX).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return cut + "\n… (index truncated: search with tools/memory_search.py)"


# ------------------------------------------------------------------ per-conversation rules ("personas")

def persona_path(home: str | os.PathLike, key: str) -> Path:
    """state/personas/<conversation key>.md. Keys are door:chat:thread or mx:!room:server; '/' never occurs
    in them, but it is replaced anyway so a key can never point outside the directory."""
    return Path(home) / "state" / "personas" / (key.replace("/", "_") + ".md")


def persona_text(home: str | os.PathLike, key: str, limit: int = PERSONA_MAX) -> str:
    try:
        text = persona_path(home, key).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return text[:limit]
