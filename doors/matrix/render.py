"""Markdown (as agents write it) -> Matrix messages: `body` is the Markdown itself (what clients without HTML show,
and what Element puts there when a person types Markdown), `formatted_body` is org.matrix.custom.html with real
paragraphs, lists, headings, quotes, code blocks and tables.

Inline formatting and the splitting of long answers are shared with the Telegram door (core.markdown). A Matrix
event may be 64 KiB in total, so a long answer is cut by its UTF-8 size; a piece whose HTML would still not fit
goes out as plain text.
"""
from __future__ import annotations

import html

from core import markdown as md
from core.markdown import escape, utf8_len  # noqa: F401  (escape is re-exported for live.py)

PIECE_BYTES = 16000          # Markdown per message; the HTML is usually 1.2-2x as large
EVENT_BUDGET = 60000         # body + formatted_body, under the 64 KiB event limit with room for the envelope
SPOILER = ("<span data-mx-spoiler>", "</span>")
SCHEMES = md.SAFE_SCHEMES + ("matrix:",)
HTML_FORMAT = "org.matrix.custom.html"


def inline(text: str) -> str:
    return md.inline(text, spoiler=SPOILER, schemes=SCHEMES)


def to_html(text: str) -> str:
    """Markdown -> Matrix HTML for one message-sized piece."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    lists: list[tuple[str, int]] = []      # open lists: (tag, depth)
    i = 0

    def close_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def close_lists(depth: int = -1) -> None:
        while lists and lists[-1][1] > depth:
            tag, _ = lists.pop()
            out.append(f"</li></{tag}>")

    while i < len(lines):
        line = lines[i]
        fence = md.FENCE.match(line)
        if fence:
            close_para()
            close_lists()
            marker, lang = fence.group(1), fence.group(2)
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker[:3]):
                body.append(lines[i])
                i += 1
            i += 1   # closing fence (or end of text)
            code = escape("\n".join(body).rstrip("\n"))
            cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
            out.append(f"<pre><code{cls}>{code}</code></pre>")
            continue
        if not line.strip():
            close_para()
            i += 1
            continue
        item = md.ITEM.match(line)
        if lists and not item and line[:1] in (" ", "\t"):
            out.append("<br>" + inline(line.strip()))     # continuation of a list item
            i += 1
            continue
        if not item:
            close_lists()
        m = md.HEADING.match(line)
        if m:
            close_para()
            level = 3 if len(m.group(1)) <= 2 else 4
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if md.RULE.match(line):
            close_para()
            out.append("<hr>")
            i += 1
            continue
        if md.QUOTE.match(line):
            close_para()
            quoted = []
            while i < len(lines) and md.QUOTE.match(lines[i]):
                quoted.append(inline(md.QUOTE.match(lines[i]).group(1)))
                i += 1
            out.append("<blockquote>" + "<br>".join(quoted).strip() + "</blockquote>")
            continue
        if md.is_table_start(lines, i):
            close_para()
            head = md.table_cells(lines[i])
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(md.table_cells(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            trs = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
            continue
        if item:
            close_para()
            indent, bullet, rest = item.groups()
            depth = len(indent.expandtabs(4)) // 2
            tag = "ol" if bullet[0].isdigit() else "ul"
            close_lists(depth)
            if lists and lists[-1][1] == depth and lists[-1][0] != tag:
                close_lists(depth - 1)
            if lists and lists[-1][1] == depth:
                out.append(f"</li><li>{inline(rest)}")
            else:
                number = bullet[:-1]
                attr = f' start="{int(number)}"' if tag == "ol" and number.isdigit() and int(number) != 1 else ""
                out.append(f"<{tag}{attr}><li>{inline(rest)}")
                lists.append((tag, depth))
            i += 1
            continue
        para.append(inline(line))
        i += 1
    close_para()
    close_lists()
    return "".join(out)


def render_messages(text: str) -> list[tuple[str, str | None]]:
    """Markdown answer -> [(body, formatted_body or None)], each piece a valid event of its own."""
    out = []
    for piece in md.split_markdown(text, PIECE_BYTES, measure=utf8_len):
        rendered = to_html(piece)
        if utf8_len(piece) + utf8_len(rendered) > EVENT_BUDGET:
            rendered = None          # cannot happen for normal text; never send an event the server rejects
        out.append((piece, rendered))
    return out


def message_content(body: str, formatted: str | None, *, notice: bool = False) -> dict:
    content = {"msgtype": "m.notice" if notice else "m.text", "body": body}
    if formatted:
        content.update(format=HTML_FORMAT, formatted_body=formatted)
    return content


def contents(text: str, *, notice: bool = False) -> list[dict]:
    """Markdown -> the m.room.message contents to send, in order."""
    return [message_content(body, formatted, notice=notice) for body, formatted in render_messages(text)]
