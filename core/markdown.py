"""Markdown as agents write it: the door-independent half of turning it into a messenger's HTML.

Every door renders the agent's Markdown into the HTML subset its messenger accepts (doors/<door>/render.py):
Telegram's subset has no lists, headings or tables, Matrix's has. What they share lives here:

* `inline()`: one line of Markdown (bold, italic, strike, code spans, links, spoilers) -> HTML that is balanced
  on its own, with everything else escaped, so no line can break the markup of a message;
* the block patterns (code fences, headings, rules, quotes, list items, table separators) the block renderers
  recognise;
* `split_markdown()`: cut a long answer into pieces under a size limit on the Markdown side, at blank lines when
  possible, closing and reopening a code block that has to be cut, so every piece renders on its own.
"""
from __future__ import annotations

import html
import re
from typing import Callable

FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+#.-]*)\s*$")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
ITEM = re.compile(r"^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
SAFE_SCHEMES = ("http://", "https://", "mailto:")
_PH = "{}"                       # private-use characters never appear in agent text
_PH_RE = re.compile("(\\d+)")


def escape(text: str) -> str:
    return html.escape(text or "", quote=False)


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def strip_html(text: str) -> str:
    """Plain-text version of rendered HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def is_table_start(lines: list[str], i: int) -> bool:
    return lines[i].lstrip().startswith("|") and i + 1 < len(lines) and bool(TABLE_SEP.match(lines[i + 1]))


def table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


# ------------------------------------------------------------------------------------------- inline

def inline(text: str, *, spoiler: tuple[str, str], schemes: tuple[str, ...] = SAFE_SCHEMES) -> str:
    """One line of Markdown -> HTML, balanced on its own. `spoiler` is the (open, close) markup for ||text||,
    `schemes` the link schemes that become links (anything else, javascript: for one, stays text)."""
    slots: list[str] = []

    def keep(fragment: str) -> str:
        slots.append(fragment)
        return _PH.format(len(slots) - 1)

    # code spans first: nothing inside them is markup
    text = re.sub(r"(`+)(.+?)\1", lambda m: keep(f"<code>{escape(m.group(2).strip())}</code>"), text)

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2).strip()
        if not url.lower().startswith(schemes):
            return m.group(0)
        return keep(f'<a href="{html.escape(url, quote=True)}">{_emphasis(escape(label), spoiler)}</a>')

    text = re.sub(r"!?\[([^\]\n]+)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)", link, text)
    text = re.sub(r"<((?:https?|tg)://[^\s>]+)>",
                  lambda m: keep(f'<a href="{html.escape(m.group(1), quote=True)}">{escape(m.group(1))}</a>')
                  if m.group(1).lower().startswith(schemes) else m.group(0),
                  text)
    text = _emphasis(escape(text), spoiler)
    for _ in range(3):   # placeholders can nest (a code span inside a link label)
        if "" not in text:
            break
        text = _PH_RE.sub(lambda m: slots[int(m.group(1))], text)
    return text


def _emphasis(text: str, spoiler: tuple[str, str]) -> str:
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w])", r"<i>\1</i>", text)
    opening, closing = spoiler
    text = re.sub(r"\|\|(?=\S)(.+?)(?<=\S)\|\|", lambda m: f"{opening}{m.group(1)}{closing}", text)
    return text


# ------------------------------------------------------------------------------------------- splitting

def _cut(s: str, limit: int, measure: Callable[[str], int]) -> int:
    """Length (in characters) of the longest prefix of `s` that measures at most `limit`."""
    if measure(s) <= limit:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(s[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return max(1, lo)


def split_markdown(md: str, limit: int, measure: Callable[[str], int] = utf16_len) -> list[str]:
    """Split Markdown into pieces that measure under `limit` (UTF-16 units for Telegram, UTF-8 bytes for
    Matrix). Cuts at blank lines when possible; a code block cut in two is closed and reopened, so both
    halves render."""
    md = (md or "").replace("\r\n", "\n").strip("\n")
    if measure(md) <= limit:
        return [md] if md.strip() else []
    pieces: list[str] = []
    cur: list[str] = []
    cur_len = 0
    fence: str | None = None          # the opening fence line while inside a code block
    last_blank = -1                   # index in cur after which a clean cut is possible

    def flush(upto: int | None = None) -> None:
        nonlocal cur, cur_len, last_blank
        take = cur if upto is None else cur[:upto]
        rest = [] if upto is None else cur[upto:]
        text = "\n".join(take).strip("\n")
        if text.strip():
            pieces.append(text)
        cur = rest
        cur_len = sum(measure(x) + 1 for x in cur)
        last_blank = -1

    for raw in md.split("\n"):
        segments = [raw]
        if measure(raw) > limit - 16:   # a monster line: hard-split it
            segments, s = [], raw
            while measure(s) > limit - 16:
                n = _cut(s, limit - 16, measure)
                segments.append(s[:n])
                s = s[n:]
            segments.append(s)
        for line in segments:
            add = measure(line) + 1
            if cur_len + add > limit - 8 and cur:
                if fence is not None:
                    cur.append(fence.strip()[:3] if fence.strip().startswith(("```", "~~~")) else "```")
                    flush()
                    cur.append(fence)
                    cur_len = measure(fence) + 1
                elif last_blank > len(cur) // 2:
                    flush(last_blank)
                else:
                    flush()
            cur.append(line)
            cur_len += add
            m = FENCE.match(line)
            if m:
                fence = None if fence is not None else line
            elif not line.strip() and fence is None:
                last_blank = len(cur)
    flush()
    return pieces
