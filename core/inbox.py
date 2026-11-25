"""Files the owner sends, whatever the door: saved under <home>/.inbox/<timestamp>_<name>, and only their path
is handed to the agent. Nothing that arrives is ever executed automatically."""
from __future__ import annotations

import re
import time
from pathlib import Path


def safe_name(raw: str | None, default: str = "file") -> str:
    """Strip any path component and hostile characters from a user-supplied filename."""
    base = Path(raw or default).name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".") or default
    return base[:80]


def inbox_path(inbox: Path, name: str, now: float | None = None) -> Path:
    """Where a received file goes: <inbox>/<YYYYmmdd-HHMMSS>_<safe name>; never overwrites an earlier file
    that arrived in the same second under the same name."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    dest = Path(inbox) / f"{stamp}_{safe_name(name)}"
    n = 1
    while dest.exists():
        dest = Path(inbox) / f"{stamp}-{n}_{safe_name(name)}"
        n += 1
    return dest


def file_prompt(kind: str, dest: Path | str, original_name: str, caption: str = "") -> str:
    """The turn prompt for a received file: where it is, and that it must be read, not run."""
    caption = (caption or "").strip()
    return (f"The owner sent a file ({kind}); it is saved at: {dest}\n"
            f"Original name: {original_name or Path(dest).name}\n"
            "Do NOT execute it automatically: read it first.\n"
            + (f"The owner's instruction with the file:\n{caption}" if caption else
               "There was no instruction: say what the file is and what you propose to do, then wait."))
