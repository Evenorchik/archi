#!/usr/bin/env python3
"""Leak scanner for the Archi repository: run before every commit and before every push.

It looks for things that must never reach a public repository:
  * secrets: bot tokens, API keys, private keys, password-like assignments;
  * public IPv4 addresses (private, loopback and documentation ranges are fine);
  * e-mail addresses (except an allow-list such as noreply addresses in commit trailers);
  * Matrix room ids;
  * any term from a PRIVATE deny-list that lives outside the repository (names, domains, hostnames,
    old project names). The deny-list path comes from $ARCHI_DENYLIST or --denylist; one term or
    regex per line, lines starting with "re:" are regular expressions, "#" starts a comment.

Usage: python3 dev/leakscan.py [--denylist FILE] [--all] [PATH ...]
Without PATH it scans the files git would commit (tracked + untracked, not ignored).
Exit code 1 when anything is found, 0 when clean.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

SECRETS = [
    ("telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("openai key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("aws key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("matrix access token", re.compile(r"\bsyt_[A-Za-z0-9_-]{20,}")),
    ("assigned secret", re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|bot[_-]?token|secret|password|passwd)\b\s*[:=]\s*['\"]?"
        r"(?!\$|<|your|example|changeme|xxx|\.\.\.|\{)[A-Za-z0-9_\-+/=]{16,}")),
]
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
ROOM_ID = re.compile(r"![A-Za-z0-9_-]{18,}(?::[A-Za-z0-9.-]+)?")
EMAIL_OK = {"noreply@anthropic.com", "you@example.com", "user@example.com", "admin@example.com",
            "name@example.com", "email@example.com"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".pdf", ".zip"}


def files_to_scan(root: Path, paths: list[str], all_files: bool) -> list[Path]:
    if paths:
        out = []
        for p in paths:
            pp = Path(p)
            out.extend([f for f in pp.rglob("*") if f.is_file()] if pp.is_dir() else [pp])
        return out
    args = ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others"]
    if not all_files:
        args.append("--exclude-standard")
    res = subprocess.run(args, capture_output=True, check=True)
    return [root / n for n in res.stdout.decode().split("\0") if n]


def load_denylist(path: str | None) -> list[tuple[str, re.Pattern]]:
    path = path or os.environ.get("ARCHI_DENYLIST")
    if not path:
        print("warning: no deny-list (set ARCHI_DENYLIST or --denylist); scanning generic patterns only",
              file=sys.stderr)
        return []
    rules = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            rules.append((line, re.compile(line[3:], re.IGNORECASE)))
        else:
            rules.append((line, re.compile(re.escape(line), re.IGNORECASE)))
    return rules


def public_ip(s: str) -> bool:
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast or ip.is_unspecified
                or ip.is_link_local or ip in ipaddress.ip_network("192.0.2.0/24")
                or ip in ipaddress.ip_network("198.51.100.0/24") or ip in ipaddress.ip_network("203.0.113.0/24"))


def scan(files: list[Path], deny: list[tuple[str, re.Pattern]], root: Path) -> int:
    hits = 0
    for f in files:
        if f.suffix.lower() in SKIP_SUFFIX or not f.exists() or f.is_symlink():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = f.relative_to(root) if f.is_relative_to(root) else f
        for n, line in enumerate(text.splitlines(), 1):
            found = []
            for name, rx in SECRETS:
                if rx.search(line):
                    found.append(name)
            for m in IPV4.finditer(line):
                if public_ip(m.group(1)):
                    found.append(f"public ip {m.group(1)}")
            for m in EMAIL.finditer(line):
                if m.group(0).lower() not in EMAIL_OK:
                    found.append(f"email {m.group(0)}")
            for m in ROOM_ID.finditer(line):
                found.append("matrix room id")
            for term, rx in deny:
                if rx.search(line):
                    found.append(f"deny-list term ({term})")
            for item in found:
                hits += 1
                print(f"{rel}:{n}: {item}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--denylist")
    ap.add_argument("--all", action="store_true", help="also scan files ignored by .gitignore")
    ap.add_argument("paths", nargs="*")
    a = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    files = files_to_scan(root, a.paths, a.all)
    hits = scan(files, load_denylist(a.denylist), root)
    print(f"{'LEAKS: ' + str(hits) if hits else 'clean'} ({len(files)} files scanned)", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
