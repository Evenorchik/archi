"""Conversation registry, persisted in state/conversations.json.

A conversation is one independent agent session: a Telegram topic, the part of a chat outside any topic
("main"), a Matrix room. Key = door + chat + thread: `tg:<chat_id>:<thread_id|main>`; a Matrix room has no
threads, its key is `mx:<room_id>` (a room id may contain ':' itself, or no server part at all).

The registry stores, per key: the session id per provider, title (and whether that title was confirmed by
a live event — only a confirmed title may be used for the existence probe), kind (dm/group), state
(open/closed/deleted), per-conversation provider/model overrides and timestamps. It also keeps a little
metadata per chat (admin rights in groups, whether the owner was seen there).

Writes are atomic (tmp + fsync + rename) and done under an exclusive file lock, because the `archi` CLI,
run by the agent during a turn, writes here too (for example after creating a topic). Deleted
conversations stay as tombstones and a full copy is appended to state/archive.jsonl.

Per-key asyncio locks serialise turns of one conversation; different conversations run in parallel.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .config import atomic_write, home

STATES = ("open", "closed", "deleted")
KINDS = ("dm", "group")
MAIN = "main"


MATRIX = "mx"          # key prefix of Matrix rooms: `mx:<room_id>`, no thread part


def make_key(door: str, chat_id: int | str, thread_id: int | str | None = None) -> str:
    if door == MATRIX:
        return f"{MATRIX}:{chat_id}"
    return f"{door}:{chat_id}:{thread_id if thread_id not in (None, '', 0, MAIN) else MAIN}"


def parse_key(key: str) -> tuple[str, str, int | None]:
    """`tg:-100123:45` -> ("tg", "-100123", 45); `tg:42:main` -> ("tg", "42", None);
    `mx:!room:example.org` -> ("mx", "!room:example.org", None)."""
    door, rest = key.split(":", 1)
    if door == MATRIX:
        return door, rest, None
    chat, _, thread = rest.rpartition(":")
    if not chat:
        return door, thread, None
    return door, chat, (None if thread == MAIN else int(thread))


@dataclass
class Conversation:
    key: str
    title: str = ""
    title_confirmed: bool = False   # title seen in a live event or set by us: safe to probe with
    name_implicit: bool = False     # Telegram default name ("New Chat") the owner never chose
    kind: str = "dm"
    state: str = "open"
    sessions: dict = field(default_factory=dict)    # provider -> session id
    provider: str = ""              # per-conversation override; "" = config PROVIDER
    model: str = ""                 # per-conversation model override; "" = config default
    effort: str = ""                # per-conversation effort override; "" = config default
    created_at: float = 0.0
    updated_at: float = 0.0
    last_turn_at: float = 0.0
    last_probe_at: float = 0.0
    deleted_at: float = 0.0

    @property
    def door(self) -> str:
        return parse_key(self.key)[0]

    @property
    def chat_id(self) -> int | str:
        """Telegram chat id (int), or the Matrix room id (str)."""
        door, chat, _thread = parse_key(self.key)
        return chat if door == MATRIX else int(chat)

    @property
    def thread_id(self) -> int | None:
        return parse_key(self.key)[2]

    @property
    def is_main(self) -> bool:
        return self.thread_id is None

    def display_title(self) -> str:
        if self.door == MATRIX:
            return self.title or "unnamed room"
        if self.is_main:
            return self.title or "main"
        return self.title or f"topic {self.thread_id}"

    @classmethod
    def from_dict(cls, data: dict) -> "Conversation":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


class Registry:
    def __init__(self, state_dir: Path | str | None = None, clock=time.time):
        self.dir = Path(state_dir) if state_dir else home() / "state"
        self.path = self.dir / "conversations.json"
        self.archive_path = self.dir / "archive.jsonl"
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._gen: dict[str, int] = {}

    # ------------------------------------------------------------------ storage
    @contextmanager
    def _locked(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".conversations.lock", "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except ValueError:
            # A corrupt registry must not take the door down; keep the bad copy for inspection.
            bad = self.path.with_suffix(f".corrupt-{int(self._clock())}.json")
            try:
                self.path.replace(bad)
            except OSError:
                pass
            data = {}
        data.setdefault("version", 1)
        data.setdefault("conversations", {})
        data.setdefault("chats", {})
        return data

    def _write(self, data: dict) -> None:
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    @contextmanager
    def _mutate(self):
        with self._locked():
            data = self._read()
            yield data
            self._write(data)

    # ------------------------------------------------------------------ queries
    def get(self, key: str) -> Conversation | None:
        raw = self._read()["conversations"].get(key)
        return Conversation.from_dict(raw) if raw else None

    def all(self, door: str | None = None, chat_id: int | str | None = None,
            include_deleted: bool = False) -> list[Conversation]:
        out = []
        for raw in self._read()["conversations"].values():
            c = Conversation.from_dict(raw)
            if door and c.door != door:
                continue
            if chat_id is not None and str(c.chat_id) != str(chat_id):
                continue
            if c.state == "deleted" and not include_deleted:
                continue
            out.append(c)
        # Telegram chat ids sort numerically, Matrix room ids as text; never compare an int with a str.
        out.sort(key=lambda c: (c.door, *((c.chat_id, "") if isinstance(c.chat_id, int) else (0, c.chat_id)),
                                c.thread_id or 0))
        return out

    def chat(self, chat_id: int | str) -> dict:
        return dict(self._read()["chats"].get(str(chat_id)) or {})

    def chats(self, door: str | None = None) -> dict[str, dict]:
        """Metadata of every known chat (Matrix rooms carry door="mx"), keyed by chat id."""
        return {cid: dict(meta) for cid, meta in self._read()["chats"].items()
                if door is None or (meta or {}).get("door") == door}

    # ------------------------------------------------------------------ mutations
    def upsert(self, key: str, **changes) -> Conversation:
        """Create the conversation if it is unknown, then apply `changes`. Returns the stored record."""
        now = self._clock()
        with self._mutate() as data:
            convs = data["conversations"]
            raw = convs.get(key)
            conv = Conversation.from_dict(raw) if raw else Conversation(key=key, created_at=now)
            for k, v in changes.items():
                if not hasattr(conv, k) or k == "key":
                    raise AttributeError(f"unknown conversation field {k!r}")
                setattr(conv, k, v)
            if conv.state not in STATES:
                raise ValueError(f"bad state {conv.state!r}")
            conv.updated_at = now
            convs[key] = asdict(conv)
        return conv

    def ensure(self, key: str, **defaults) -> tuple[Conversation, bool]:
        """Return (conversation, created). `defaults` are applied only when it is new."""
        existing = self.get(key)
        if existing is not None:
            return existing, False
        return self.upsert(key, **defaults), True

    def update_chat(self, chat_id: int | str, **changes) -> dict:
        with self._mutate() as data:
            meta = data["chats"].setdefault(str(chat_id), {})
            meta.update(changes)
            meta["updated_at"] = self._clock()
            return dict(meta)

    def set_session(self, key: str, provider: str, session_id: str | None, gen: int | None = None) -> bool:
        """Store the session id a turn ended with. A /new that happened while the turn ran bumped the
        generation; writing the old id back would silently resurrect the context the owner dropped."""
        if gen is not None and gen != self.generation(key):
            return False
        if not session_id:
            return False
        with self._mutate() as data:
            raw = data["conversations"].get(key)
            if raw is None:
                raw = asdict(Conversation(key=key, created_at=self._clock()))
                data["conversations"][key] = raw
            raw.setdefault("sessions", {})[provider] = session_id
            raw["updated_at"] = self._clock()
        return True

    def clear_session(self, key: str, provider: str | None = None) -> bool:
        """Forget the session(s) of this conversation. Returns True if there was one."""
        self._gen[key] = self.generation(key) + 1
        had = False
        with self._mutate() as data:
            raw = data["conversations"].get(key)
            if raw:
                sessions = raw.setdefault("sessions", {})
                if provider:
                    had = sessions.pop(provider, None) is not None
                else:
                    had = bool(sessions)
                    sessions.clear()
                raw["updated_at"] = self._clock()
        return had

    def mark_deleted(self, key: str, reason: str = "") -> Conversation | None:
        """Tombstone the conversation and append a full copy to the archive. None if unknown or
        already deleted (so a caller announces a deletion exactly once)."""
        now = self._clock()
        with self._mutate() as data:
            raw = data["conversations"].get(key)
            if not raw or raw.get("state") == "deleted":
                return None
            raw["state"] = "deleted"
            raw["deleted_at"] = now
            raw["updated_at"] = now
            conv = Conversation.from_dict(raw)
            self.archive_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.archive_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({**raw, "archived_at": now, "reason": reason}, ensure_ascii=False) + "\n")
        return conv

    # ------------------------------------------------------------------ concurrency
    def lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def busy(self, key: str) -> bool:
        lock = self._locks.get(key)
        return bool(lock and lock.locked())

    def generation(self, key: str) -> int:
        return self._gen.get(key, 0)
