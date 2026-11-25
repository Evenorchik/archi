"""Markdown (as agents write it) -> Telegram HTML, and splitting into messages under the 4096 limit.

Telegram's HTML is a small strict subset: <b> <i> <s> <u> <code> <pre> <a> <blockquote>; anything else, or
an unbalanced tag, makes the whole message fail. So the converter is deliberately simple and line based:
every tag it opens is closed on the same line or block, and everything else is escaped. Long answers are
split on the Markdown side (at blank lines, never inside a code block without closing and reopening it),
then every piece is rendered on its own, so each message is valid by construction.
"""
from __future__ import annotations

import html
import re

LIMIT = 4096
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+#.-]*)\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_ITEM = re.compile(r"^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_PH = "{}"
_PH_RE = re.compile("(\\d+)")
_SAFE_SCHEMES = ("http://", "https://", "tg://", "mailto:")


def escape(text: str) -> str:
    return html.escape(text or "", quote=False)


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def strip_html(text: str) -> str:
    """Plain-text version of rendered HTML (fallback when Telegram rejects the markup)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def visible_len(rendered: str) -> int:
    return utf16_len(strip_html(rendered))


# ------------------------------------------------------------------------------------------- inline

def inline(text: str) -> str:
    """One line of Markdown -> Telegram HTML (balanced on its own)."""
    slots: list[str] = []

    def keep(fragment: str) -> str:
        slots.append(fragment)
        return _PH.format(len(slots) - 1)

    # code spans first: nothing inside them is markup
    text = re.sub(r"(`+)(.+?)\1", lambda m: keep(f"<code>{escape(m.group(2).strip())}</code>"), text)

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2).strip()
        if not url.lower().startswith(_SAFE_SCHEMES):
            return m.group(0)
        return keep(f'<a href="{html.escape(url, quote=True)}">{_emphasis(escape(label))}</a>')

    text = re.sub(r"!?\[([^\]\n]+)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)", link, text)
    text = re.sub(r"<((?:https?|tg)://[^\s>]+)>",
                  lambda m: keep(f'<a href="{html.escape(m.group(1), quote=True)}">{escape(m.group(1))}</a>'),
                  text)
    text = _emphasis(escape(text))
    for _ in range(3):   # placeholders can nest (a code span inside a link label)
        if "" not in text:
            break
        text = _PH_RE.sub(lambda m: slots[int(m.group(1))], text)
    return text


def _emphasis(text: str) -> str:
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w])", r"<i>\1</i>", text)
    text = re.sub(r"\|\|(?=\S)(.+?)(?<=\S)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)
    return text


# ------------------------------------------------------------------------------------------- blocks

def to_html(md: str) -> str:
    """Markdown -> Telegram HTML for one message-sized piece."""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    blank = False

    def emit(s: str) -> None:
        nonlocal blank
        out.append(s)
        blank = False

    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            marker, lang = fence.group(1), fence.group(2)
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker[:3]):
                body.append(lines[i])
                i += 1
            i += 1   # closing fence (or end of text)
            code = escape("\n".join(body).rstrip("\n"))
            cls = f' class="language-{escape(lang)}"' if lang else ""
            emit(f"<pre><code{cls}>{code}</code></pre>" if cls else f"<pre>{code}</pre>")
            continue
        if not line.strip():
            if out and not blank:
                out.append("")
                blank = True
            i += 1
            continue
        m = _HEADING.match(line)
        if m:
            emit(f"<b>{inline(m.group(1))}</b>")
            i += 1
            continue
        if _RULE.match(line):
            emit("——————")
            i += 1
            continue
        if _QUOTE.match(line):
            quoted = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                quoted.append(inline(_QUOTE.match(lines[i]).group(1)))
                i += 1
            emit("<blockquote>" + "\n".join(quoted).strip("\n") + "</blockquote>")
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            rows = []
            header = True
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if _TABLE_SEP.match(lines[i]):
                    i += 1
                    continue
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rendered = " | ".join(inline(c) for c in cells)
                rows.append(f"<b>{rendered}</b>" if header else rendered)
                header = False
                i += 1
            emit("\n".join(rows))
            continue
        m = _ITEM.match(line)
        if m:
            indent, bullet, rest = m.groups()
            depth = len(indent.expandtabs(4)) // 2
            mark = bullet if bullet[0].isdigit() else ("•" if depth == 0 else "◦")
            emit(f"{'  ' * depth}{mark} {inline(rest)}")
            i += 1
            continue
        emit(inline(line))
        i += 1
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


# ------------------------------------------------------------------------------------------- splitting

def split_markdown(md: str, limit: int = 3900) -> list[str]:
    """Split Markdown into pieces whose rendered text stays under `limit` UTF-16 units. Cuts at blank
    lines when possible; a code block cut in two is closed and reopened, so both halves render."""
    md = (md or "").replace("\r\n", "\n").strip("\n")
    if utf16_len(md) <= limit:
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
        cur_len = sum(utf16_len(x) + 1 for x in cur)
        last_blank = -1

    for raw in md.split("\n"):
        segments = [raw]
        if utf16_len(raw) > limit - 16:   # a monster line: hard-split it
            segments, s = [], raw
            while utf16_len(s) > limit - 16:
                segments.append(s[: limit - 16])
                s = s[limit - 16:]
            segments.append(s)
        for line in segments:
            add = utf16_len(line) + 1
            if cur_len + add > limit - 8 and cur:
                if fence is not None:
                    cur.append(fence.strip()[:3] if fence.strip().startswith(("```", "~~~")) else "```")
                    flush()
                    cur.append(fence)
                    cur_len = utf16_len(fence) + 1
                elif last_blank > len(cur) // 2:
                    flush(last_blank)
                else:
                    flush()
            cur.append(line)
            cur_len += add
            m = _FENCE.match(line)
            if m:
                fence = None if fence is not None else line
            elif not line.strip() and fence is None:
                last_blank = len(cur)
    flush()
    return pieces


def render_messages(md: str, limit: int = LIMIT) -> list[str]:
    """Markdown answer -> list of Telegram HTML messages, each within the limit."""
    out = []
    for piece in split_markdown(md, limit=limit - 196):
        rendered = to_html(piece)
        if visible_len(rendered) > limit:   # should not happen; never send something Telegram rejects
            plain = strip_html(rendered)
            out += [escape(plain[i:i + limit - 96]) for i in range(0, len(plain), limit - 96)]
        elif rendered.strip():
            out.append(rendered)
    return out
