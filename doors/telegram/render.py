"""Markdown (as agents write it) -> Telegram HTML, and splitting into messages under the 4096 limit.

Telegram's HTML is a small strict subset: <b> <i> <s> <u> <code> <pre> <a> <blockquote>; anything else, or
an unbalanced tag, makes the whole message fail. So the converter is deliberately simple and line based:
every tag it opens is closed on the same line or block, and everything else is escaped. Long answers are
split on the Markdown side (at blank lines, never inside a code block without closing and reopening it),
then every piece is rendered on its own, so each message is valid by construction.
"""
from __future__ import annotations

from core import markdown as _md
from core.markdown import escape, strip_html, utf16_len  # noqa: F401  (re-exported: doors and tests use them)

LIMIT = 4096
_FENCE, _HEADING, _RULE, _QUOTE, _ITEM = _md.FENCE, _md.HEADING, _md.RULE, _md.QUOTE, _md.ITEM
_TABLE_SEP = _md.TABLE_SEP
_SAFE_SCHEMES = _md.SAFE_SCHEMES + ("tg://",)
_SPOILER = ("<tg-spoiler>", "</tg-spoiler>")


def visible_len(rendered: str) -> int:
    return utf16_len(strip_html(rendered))


# ------------------------------------------------------------------------------------------- inline

def inline(text: str) -> str:
    """One line of Markdown -> Telegram HTML (balanced on its own)."""
    return _md.inline(text, spoiler=_SPOILER, schemes=_SAFE_SCHEMES)


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
            emit(f"<b>{inline(m.group(2))}</b>")
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
    """Split Markdown into pieces whose rendered text stays under `limit` UTF-16 units (see core.markdown)."""
    return _md.split_markdown(md, limit, measure=utf16_len)


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
