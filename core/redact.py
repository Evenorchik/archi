"""What looks like a secret, for every part of Archi that must not copy one somewhere: the learning loop
(memory files, skill edits, the digest a reviewer reads) and durable jobs (the environment kept in a job's
spec). Known token formats, private keys, URLs with a password, and `password = <long value>` assignments;
plain words after "token:" ("token: stored in the config") are not secrets.
"""
from __future__ import annotations

import re

SECRET_RE = re.compile(
    r"(\bsk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{30,}|\bxox[abpr]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{35}\b|\bsyt_[A-Za-z0-9_-]{20,}|\bhf_[A-Za-z0-9]{20,}"
    r"|\b\d{8,10}:[A-Za-z0-9_-]{35}\b|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"
    r"|\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|bot[_-]?token|secret|password|passwd)\b\s*[:=]\s*['\"]?"
    r"(?=[A-Za-z0-9_+/=-]*\d)(?=[A-Za-z0-9_+/=-]*[A-Za-z])[A-Za-z0-9_+/=-]{16,})", re.I)
# Environment variable NAMES that hold secrets by convention (STRIPE_KEY, DB_PASS, SENTRY_DSN, …).
SECRET_NAME_RE = re.compile(r"(?i)(TOKEN|SECRET|PASS|API_?KEY|_KEY\b|^KEY\b|PRIVATE|CREDENTIAL|COOKIE|AUTH|DSN"
                            r"|DATABASE_URL)")


def redact(text: str) -> str:
    return SECRET_RE.sub("[secret removed]", text or "")


def looks_secret(text: str) -> bool:
    return bool(SECRET_RE.search(text or ""))


def secret_env(name: str, value: str) -> bool:
    """True when an environment variable must not be copied anywhere (by its name or by its value)."""
    return bool(SECRET_NAME_RE.search(name or "")) or looks_secret(str(value or ""))
