from html.parser import HTMLParser

from doors.telegram import render

ALLOWED = {"b", "i", "s", "u", "code", "pre", "a", "blockquote", "tg-spoiler"}


class Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.bad = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.bad.append(tag)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.bad.append("/" + tag)


def balanced(html: str) -> bool:
    p = Balance()
    p.feed(html)
    p.close()
    return not p.bad and not p.stack


def test_inline_formatting_and_escaping():
    out = render.to_html("**bold** and *it* and `a<b>` and ~~old~~ 2 < 3 & snake_case_name")
    assert out == ("<b>bold</b> and <i>it</i> and <code>a&lt;b&gt;</code> and <s>old</s> 2 &lt; 3 &amp; "
                   "snake_case_name")
    assert balanced(out)


def test_links_safe_schemes_only():
    assert render.to_html("[site](https://example.com/a?b=1&c=2)") == \
        '<a href="https://example.com/a?b=1&amp;c=2">site</a>'
    assert "<a" not in render.to_html("[x](javascript:alert(1))")


def test_blocks():
    md = "# Title\n\n- one\n- two\n  - nested\n1. first\n\n> quoted\n\n```python\nx = 1 < 2\n```\n---\n"
    out = render.to_html(md)
    assert out.startswith("<b>Title</b>")
    assert "• one" in out and "  ◦ nested" in out and "1. first" in out
    assert "<blockquote>quoted</blockquote>" in out
    assert '<pre><code class="language-python">x = 1 &lt; 2</code></pre>' in out
    assert balanced(out)


def test_table_becomes_rows():
    out = render.to_html("| a | b |\n|---|---|\n| 1 | 2 |")
    assert out == "<b>a | b</b>\n1 | 2"


def test_unclosed_fence_is_still_valid():
    out = render.to_html("text\n```\ncode without end")
    assert out.endswith("<pre>code without end</pre>") and balanced(out)


def test_split_long_text_and_code():
    para = "word " * 300
    code = "\n".join(f"line {i} <tag>" for i in range(900))
    md = f"{para}\n\n```\n{code}\n```\n\n{para}"
    msgs = render.render_messages(md)
    assert len(msgs) > 2
    for m in msgs:
        assert render.visible_len(m) <= 4096
        assert balanced(m), m[:200]
    joined = "".join(render.strip_html(m) for m in msgs)
    assert "line 0 <tag>" in joined and "line 899 <tag>" in joined


def test_monster_line_is_split():
    msgs = render.render_messages("x" * 10000)
    assert len(msgs) >= 3 and all(render.visible_len(m) <= 4096 for m in msgs)


def test_utf16_length_counts_emoji_as_two():
    assert render.utf16_len("😀a") == 3


def test_shared_splitter_measures_utf8_for_matrix():
    from core.markdown import split_markdown, utf8_len
    text = "ж" * 30000                               # one monster line, 2 bytes per character
    parts = split_markdown(text, 16000, measure=utf8_len)
    assert len(parts) >= 4 and all(utf8_len(p) <= 16000 for p in parts) and "".join(parts) == text


def test_matrix_html_blocks():
    from doors.matrix import render as mxr
    out = mxr.to_html("## Plan\n\n1. one\n2. two\n   - sub\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n||spoiler||")
    assert out.startswith("<h3>Plan</h3><ol><li>one</li><li>two<ul><li>sub</li></ul></li></ol>")
    assert "<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>" in out
    assert "<span data-mx-spoiler>spoiler</span>" in out and matrix_balanced(out)


MATRIX_TAGS = {"font", "del", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "p", "a", "ul", "ol", "sup", "sub",
               "li", "b", "i", "u", "strong", "em", "s", "code", "hr", "br", "div", "table", "thead", "tbody", "tr",
               "th", "td", "caption", "pre", "span"}          # the HTML subset the Matrix spec recommends


def matrix_balanced(html: str) -> bool:
    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.bad = [], []

        def handle_starttag(self, tag, attrs):
            if tag not in MATRIX_TAGS:
                self.bad.append(tag)
            if tag not in ("br", "hr"):
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if not self.stack or self.stack.pop() != tag:
                self.bad.append("/" + tag)

    p = P()
    p.feed(html)
    p.close()
    return not p.bad and not p.stack


def test_matrix_long_answers_are_valid_pieces():
    from doors.matrix import render as mxr
    md = "# Report\n\n" + "\n".join(f"- item {i} with **bold** <tag> & `code`" for i in range(3000)) + \
         "\n\n```\n" + "\n".join(f"line {i} <x>" for i in range(2000)) + "\n```\n"
    pieces = mxr.render_messages(md)
    assert len(pieces) > 3
    for body, html in pieces:
        assert html and matrix_balanced(html), html[:300]
        assert len(body.encode()) + len(html.encode()) < 60000
