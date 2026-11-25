#!/usr/bin/env python3
"""
memory_search — full-text search over the agent's memory (SQLite FTS5).

Instead of re-reading every note, search: sessions/, knowledge/ and the Claude Code memory directory of this
agent home. The index is rebuilt on every run (the corpus is small), so it is always fresh.

Usage:
  python3 tools/memory_search.py search "<query>" [--limit N]
  python3 tools/memory_search.py index      # rebuild the index, print the count
  python3 tools/memory_search.py stats      # what is indexed
"""
import os
import re
import sqlite3
import sys

# Paths come from the script's location, not hard-coded:
#   BASE   = the agent home (one level above tools/)
#   slug   = the key Claude Code derives from the working directory (every non-alphanumeric
#            character becomes '-'), e.g. /opt/archi -> -opt-archi
#   MEMORY = ~/.claude/projects/<slug>/memory (Claude Code's own memory store)
# Override with ARCHI_HOME / ARCHI_MEMORY_DIR.
BASE = os.environ.get("ARCHI_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_slug = re.sub(r"[^A-Za-z0-9]", "-", BASE)
MEMORY_DIR = os.environ.get(
    "ARCHI_MEMORY_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "projects", _slug, "memory"),
)
ROOTS = [
    os.path.join(BASE, "sessions"),
    os.path.join(BASE, "knowledge"),
    MEMORY_DIR,
]
DB_PATH = os.path.join(BASE, "state", "memory_index.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
EXTS = (".md", ".txt")
MAX_BYTES = 400_000  # skip abnormally large files


def iter_files(roots):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.startswith("."):
                    continue
                if not name.lower().endswith(EXTS):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(path) > MAX_BYTES:
                        continue
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                title = first_heading(text) or name
                yield path, title, text


def first_heading(text):
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
        if s:
            return s[:80]
    return ""


def build_index(conn):
    conn.executescript(
        """
        DROP TABLE IF EXISTS docs;
        CREATE VIRTUAL TABLE docs USING fts5(
            path, title, body,
            tokenize = "unicode61 remove_diacritics 2"
        );
        """
    )
    n = 0
    for path, title, body in iter_files(ROOTS):
        conn.execute(
            "INSERT INTO docs (path, title, body) VALUES (?, ?, ?)",
            (path, title, body),
        )
        n += 1
    conn.commit()
    return n


def make_match(query):
    """Turn an arbitrary query into a safe FTS5 MATCH: take the words (letters/digits), each as a prefix term,
    joined with OR."""
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        return None
    return " OR ".join(f'"{t}"*' for t in terms)


def cmd_search(conn, query, limit):
    match = make_match(query)
    if not match:
        print("Empty query.")
        return
    rows = conn.execute(
        """
        SELECT path, title,
               snippet(docs, 2, '«', '»', ' … ', 12) AS snip,
               bm25(docs) AS rank
        FROM docs
        WHERE docs MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match, limit),
    ).fetchall()
    if not rows:
        print(f"Nothing found for: {query}")
        return
    print(f"Found {len(rows)} (query: {query})\n")
    for i, (path, title, snip, rank) in enumerate(rows, 1):
        snip = " ".join(snip.split())
        print(f"{i}. {title}")
        print(f"   {path}")
        print(f"   {snip}\n")


def cmd_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    print(f"Indexed documents: {total}")
    for root in ROOTS:
        c = conn.execute(
            "SELECT COUNT(*) FROM docs WHERE path LIKE ?", (root + "%",)
        ).fetchone()[0]
        print(f"  {root}: {c}")


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    conn = sqlite3.connect(DB_PATH)
    count = build_index(conn)

    if cmd == "index":
        print(f"Index rebuilt: {count} documents -> {DB_PATH}")
        return 0
    if cmd == "stats":
        cmd_stats(conn)
        return 0
    if cmd == "search":
        rest = argv[1:]
        limit = 8
        if "--limit" in rest:
            i = rest.index("--limit")
            try:
                limit = int(rest[i + 1])
            except (IndexError, ValueError):
                limit = 8
            rest = rest[:i] + rest[i + 2:]
        query = " ".join(rest).strip()
        if not query:
            print("Usage: memory_search.py search \"<query>\"")
            return 1
        cmd_search(conn, query, limit)
        return 0

    print(f"Unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
