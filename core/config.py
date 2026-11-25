"""Archi configuration: `config/archi.env` (KEY=VALUE, mode 0600) and the optional `config/identity.json`.

The file is the single source of truth. Both the door and the `archi` CLI (run by the agent itself during a
turn) read and write it, so every read checks the file's mtime and every write is an atomic rewrite under
an exclusive lock: a reader never sees a half-written file and two writers never lose each other's keys.

ARCHI_HOME (environment) is the agent's home: its working directory, config, state and inbox. It defaults
to the directory this code lives in, which is what an installed agent uses (/opt/archi).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent

KNOWN_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_USERNAME",
    "OWNER_ID",
    "PAIRING_CODE",
    "PROVIDER",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CODEX_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "AGENT_NAME",
    "MATRIX_HOMESERVER",
    "MATRIX_USER_ID",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_DEVICE_ID",
    "MATRIX_OWNER_ID",
)
DEFAULTS = {
    "PROVIDER": "claude",
    "AGENT_NAME": "Archi",
    "CLAUDE_MODEL": "",      # empty = the CLI's own default model
    "CLAUDE_EFFORT": "",     # empty = the model's default reasoning effort
    "CODEX_MODEL": "",
    "VOICE_MODEL": "gpt-4o-mini-transcribe",
    "TTS_MODEL": "gpt-4o-mini-tts",
    "TTS_VOICE": "alloy",
    "TIMEZONE": "",          # empty = the server's local time zone
    "TG_PROBE_INTERVAL": "900",
    "TG_AUTO_TITLE": "1",
}
SECRET_KEYS = {"TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "PAIRING_CODE", "CLAUDE_CODE_OAUTH_TOKEN",
               "MATRIX_ACCESS_TOKEN"}
PROVIDERS = ("claude", "codex")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PLAIN_VALUE = re.compile(r"^[A-Za-z0-9_@:/.+,=%-]*$")


def home() -> Path:
    """The agent's home directory (working directory of every turn, config, state)."""
    return Path(os.environ.get("ARCHI_HOME") or CODE_DIR).resolve()


def _parse_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            return str(json.loads(raw))
        except ValueError:
            return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw


def _format_value(value: str) -> str:
    return value if _PLAIN_VALUE.match(value) else json.dumps(value, ensure_ascii=False)


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        k, v = s.split("=", 1)
        k = k.strip()
        if _KEY_RE.match(k):
            out[k] = _parse_value(v)
    return out


class Config:
    """Read/write access to config/archi.env. Cheap to construct; re-reads the file when it changed."""

    def __init__(self, home_dir: Path | str | None = None):
        self.home = Path(home_dir).resolve() if home_dir else home()
        self.dir = self.home / "config"
        self.path = self.dir / "archi.env"
        self.identity_path = self.dir / "identity.json"
        self._cache: dict[str, str] = {}
        self._mtime: float | None = None

    # ------------------------------------------------------------------ reading
    def _load(self) -> dict[str, str]:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._cache, self._mtime = {}, None
            return self._cache
        if mtime != self._mtime:
            self._cache = parse_env(self.path.read_text(encoding="utf-8"))
            self._mtime = mtime
        return self._cache

    def get(self, key: str, default: str | None = None) -> str:
        val = self._load().get(key)
        if val is None or val == "":
            if default is not None:
                return default
            return DEFAULTS.get(key, "")
        return val

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)) or default)
        except ValueError:
            return default

    def all(self) -> dict[str, str]:
        return dict(self._load())

    # ------------------------------------------------------------------ writing
    @contextmanager
    def _locked(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.dir / ".archi.env.lock"
        with open(lock_path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def set(self, key: str, value: str | None) -> None:
        """Set (or, with value None, remove) one key. Comments, order and other keys survive."""
        self.update({key: value})

    def update(self, changes: dict[str, str | None]) -> None:
        for k in changes:
            if not _KEY_RE.match(k):
                raise ValueError(f"invalid config key: {k!r}")
        with self._locked():
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = ["# Archi configuration. KEY=VALUE per line; this file holds secrets (mode 0600)."]
            pending = dict(changes)
            out: list[str] = []
            for line in lines:
                s = line.strip()
                body = s[7:].lstrip() if s.startswith("export ") else s
                k = body.split("=", 1)[0].strip() if "=" in body and not body.startswith("#") else ""
                if k in pending:
                    v = pending.pop(k)
                    if v is not None:
                        out.append(f"{k}={_format_value(str(v))}")
                    continue
                out.append(line)
            for k, v in pending.items():
                if v is not None:
                    out.append(f"{k}={_format_value(str(v))}")
            _atomic_write(self.path, "\n".join(out) + "\n", mode=0o600)
        self._mtime = None

    # ------------------------------------------------------------------ identity
    def identity(self) -> dict:
        """Optional config/identity.json: {"name": ..., "persona": "...", "language": "..."}."""
        try:
            data = json.loads(self.identity_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def agent_name(self) -> str:
        return str(self.identity().get("name") or self.get("AGENT_NAME") or "Archi")

    def has_name(self) -> bool:
        """False until the owner gave the agent a name (the "First run" of CLAUDE.md is still pending)."""
        return bool(str(self.identity().get("name") or "").strip() or self.all().get("AGENT_NAME", "").strip())

    def owner_id(self) -> int | None:
        raw = self.get("OWNER_ID").strip()
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    def provider(self) -> str:
        p = self.get("PROVIDER").strip().lower()
        return p if p in PROVIDERS else "claude"


def mask(key: str, value: str) -> str:
    """Display form of a config value: secrets are never printed in full."""
    if key in SECRET_KEYS and value:
        return f"<set, {len(value)} chars, ends with {value[-4:]}>" if len(value) > 8 else "<set>"
    return value


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write `text` to `path` so that readers see either the old or the new content, never a mix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


atomic_write = _atomic_write
