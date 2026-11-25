"""Markers the agent writes into an answer to send media: `[FILE /abs/path]` and `[VOICE lang="xx"]…[/VOICE]`.

Both must start a line, so an inline example in prose is never mistaken for a request. File paths are
ASCII Unix paths without spaces, as in the instructions given to the agent.
"""
from __future__ import annotations

import re

VOICE_RE = re.compile(
    r'(?m)^\s*\[VOICE\s+lang=["\'](\w+)["\'](?:\s+speed=["\']([0-9.]+)["\'])?\s*\](.*?)\[/VOICE\]',
    re.DOTALL,
)
FILE_RE = re.compile(r'(?im)^\s*\[FILE[:\s]\s*(/[a-zA-Z0-9_./@+-]+)\s*\](?:\s*\[/FILE\])?')


def extract_files(text: str) -> tuple[str, list[str]]:
    paths: list[str] = []
    for m in FILE_RE.finditer(text or ""):
        p = m.group(1).strip()
        if p not in paths:
            paths.append(p)
    return FILE_RE.sub("", text or "").strip(), paths


def extract_voice(text: str) -> tuple[str, list[tuple[str, str, float]]]:
    """-> (text without voice blocks, [(lang, speech, speed), ...])."""
    blocks = []
    for m in VOICE_RE.finditer(text or ""):
        speech = m.group(3).strip()
        if speech:
            try:
                speed = float(m.group(2)) if m.group(2) else 1.0
            except ValueError:
                speed = 1.0
            blocks.append((m.group(1), speech, max(0.25, min(4.0, speed))))
    return VOICE_RE.sub("", text or "").strip(), blocks


def split_answer(text: str) -> tuple[str, list[str], list[tuple[str, str, float]]]:
    """-> (clean text, file paths, voice blocks)."""
    no_files, files = extract_files(text)
    clean, voices = extract_voice(no_files)
    return clean, files, voices
