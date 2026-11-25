"""Matrix client-server API access: the door's async client, a stdlib client, homeserver discovery.

* `MxApi` (async, used by the door) wraps matrix-nio's AsyncClient WITHOUT end-to-end encryption. Sends,
  redactions, typing, uploads and downloads go through nio's typed calls; /sync, joins and leaves go through
  nio's HTTP session as plain JSON requests: the door handles the event JSON the spec documents, one event nio
  cannot parse never wedges the sync loop, room ids of room version 12 (no server part) work, and joins carry
  the JSON body strict servers require.
* `SyncApi` (stdlib only) is what the `archi` CLI, `archi setup matrix` and doors.matrix.notify use: they
  must work without the venv.
* `resolve_homeserver()` turns what the owner typed into the client-API base URL, following the
  /.well-known/matrix/client delegation that self-hosted servers use.

Both clients raise MxError(errcode, error, status).
"""
from __future__ import annotations

import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MXID_RE = re.compile(r"^@[^:\s]+:[^\s]+$")
TIMEOUT = 30
UA = "Archi"


class MxError(Exception):
    def __init__(self, errcode: str, error: str = "", status: int = 0, retry_after_ms: int | None = None):
        super().__init__(error or errcode)
        self.errcode = errcode or "M_UNKNOWN"
        self.error = error or ""
        self.status = status or 0
        self.retry_after_ms = retry_after_ms

    @property
    def fatal(self) -> bool:
        """The access token is not (or no longer) valid: nothing will work until `archi setup matrix`."""
        return self.errcode in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN") or self.status == 401

    @property
    def not_in_room(self) -> bool:
        """The bot is not (or no longer) a member of the room it tried to act in."""
        return self.errcode == "M_FORBIDDEN" and bool(
            re.search(r"not (in|joined to|a member of|part of)|not joined|isn't in|is not in", self.error, re.I))

    def __str__(self) -> str:
        return f"{self.errcode}: {self.error}" if self.error else self.errcode


def is_mxid(value: str) -> bool:
    return bool(MXID_RE.match((value or "").strip()))


def room_link(room_id: str) -> str:
    return f"https://matrix.to/#/{urllib.parse.quote(room_id, safe='')}"


def txn_id() -> str:
    return f"archi{time.time_ns()}{secrets.token_hex(3)}"


# =========================================================================================== stdlib client

class SyncApi:
    """Minimal stdlib client (the `archi` CLI and setup must work without the venv)."""

    def __init__(self, homeserver: str, token: str = "", timeout: float = TIMEOUT):
        self.homeserver = homeserver.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict | None = None, query: dict | None = None,
             timeout: float | None = None) -> dict:
        url = self.homeserver + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", UA)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(raw)
            except ValueError:
                err = {}
            if not isinstance(err, dict):
                err = {}
            raise MxError(str(err.get("errcode") or f"HTTP_{e.code}"), str(err.get("error") or raw[:200]),
                          e.code, err.get("retry_after_ms")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise MxError("ARCHI_NETWORK", f"cannot reach {self.homeserver}: {getattr(e, 'reason', e)}") from None
        try:
            out = json.loads(raw or "{}")
        except ValueError:
            raise MxError("ARCHI_BAD_RESPONSE", f"not JSON: {raw[:200]}") from None
        return out if isinstance(out, dict) else {}

    # ------------------------------------------------------------------ account
    def login(self, user: str, password: str, device_name: str = "Archi") -> dict:
        """-> {user_id, access_token, device_id}. `user` is a localpart or a full @name:server."""
        res = self.call("POST", "/_matrix/client/v3/login", {
            "type": "m.login.password", "identifier": {"type": "m.id.user", "user": user},
            "password": password, "initial_device_display_name": device_name})
        return {"user_id": res.get("user_id", ""), "access_token": res.get("access_token", ""),
                "device_id": res.get("device_id", "")}

    def whoami(self) -> dict:
        return self.call("GET", "/_matrix/client/v3/account/whoami")

    # ------------------------------------------------------------------ rooms
    def rooms(self) -> list[dict]:
        """Joined and invited rooms with their names and whether they are encrypted (one immediate sync)."""
        flt = json.dumps({"presence": {"types": []}, "account_data": {"types": []},
                          "room": {"timeline": {"limit": 1}, "ephemeral": {"types": []},
                                   "state": {"types": ["m.room.name", "m.room.encryption"]}}})
        res = self.call("GET", "/_matrix/client/v3/sync", query={"timeout": "0", "filter": flt}, timeout=60)
        rooms = res.get("rooms") or {}
        out = []
        for membership, section, field in (("join", "join", "state"), ("invite", "invite", "invite_state")):
            for rid, data in (rooms.get(section) or {}).items():
                events = list(((data.get(field) or {}).get("events")) or [])
                events += list(((data.get("timeline") or {}).get("events")) or [])
                name, enc = "", False
                for ev in events:
                    if ev.get("type") == "m.room.name":
                        name = str((ev.get("content") or {}).get("name") or name)
                    elif ev.get("type") == "m.room.encryption":
                        enc = True
                out.append({"room_id": rid, "name": name, "membership": membership, "encrypted": enc})
        return out

    def create_room(self, name: str, invite: list[str], topic: str = "") -> str:
        """A private room without encryption. Invitees get the creator's power level (trusted_private_chat),
        so the owner can manage the room the agent made."""
        body: dict[str, Any] = {"name": name, "preset": "trusted_private_chat", "visibility": "private",
                                "invite": [u for u in invite if u]}
        if topic:
            body["topic"] = topic
        return str(self.call("POST", "/_matrix/client/v3/createRoom", body).get("room_id") or "")

    def state(self, room_id: str, event_type: str, state_key: str = "") -> dict | None:
        path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/state/"
                f"{urllib.parse.quote(event_type, safe='')}/{urllib.parse.quote(state_key, safe='')}")
        try:
            return self.call("GET", path)
        except MxError as e:
            if e.status == 404 or e.errcode == "M_NOT_FOUND":
                return None
            raise

    def set_name(self, room_id: str, name: str) -> None:
        self.call("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/state/m.room.name/",
                  {"name": name})

    def send(self, room_id: str, content: dict, event_type: str = "m.room.message") -> str:
        path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/send/"
                f"{urllib.parse.quote(event_type, safe='')}/{txn_id()}")
        return str(self.call("PUT", path, content).get("event_id") or "")

    def join(self, room_id: str) -> str:
        res = self.call("POST", f"/_matrix/client/v3/join/{urllib.parse.quote(room_id, safe='')}", {})
        return str(res.get("room_id") or room_id)

    def leave(self, room_id: str) -> None:
        self.call("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/leave", {})


# =========================================================================================== discovery

def _probe_json(url: str, timeout: float = 10) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def resolve_homeserver(server: str, timeout: float = 10) -> tuple[str, bool]:
    """What the owner typed (a domain, a server name or a URL) -> (client-API base URL, delegated).

    Self-hosted servers routinely delegate: you sign in as @you:example.org while the client API lives at
    matrix.example.org, published at /.well-known/matrix/client (the client API has no redirect for it). The
    delegated base URL is tried first, then the address itself; only one that answers
    /_matrix/client/versions is accepted."""
    raw = (server or "").strip().rstrip("/")
    if not raw:
        raise MxError("ARCHI_NO_SERVER", "no homeserver given")
    if raw.startswith("@") and ":" in raw:
        raw = raw.split(":", 1)[1]           # a full user id: its server part
    base = raw if raw.startswith(("http://", "https://")) else "https://" + raw
    wk = _probe_json(base + "/.well-known/matrix/client", timeout)
    delegated = str(((wk or {}).get("m.homeserver") or {}).get("base_url") or "").strip().rstrip("/")
    for cand in [c for c in (delegated, base) if c]:
        versions = _probe_json(cand + "/_matrix/client/versions", timeout)
        if versions and isinstance(versions.get("versions"), list):
            return cand, bool(delegated) and cand == delegated
    raise MxError("ARCHI_NO_SERVER", f"no Matrix homeserver answered at {base}"
                  + (f" or at {delegated} (from its .well-known)" if delegated else "")
                  + ": check the address, and that its TLS certificate is valid")


# =========================================================================================== door client

class MxApi:
    """Async access for the door, through matrix-nio (no end-to-end encryption)."""

    def __init__(self, homeserver: str, user_id: str, token: str, device_id: str = ""):
        from nio import AsyncClient, AsyncClientConfig   # imported here: the CLI and the tests never need nio
        self.homeserver = homeserver.rstrip("/")
        config = AsyncClientConfig(max_limit_exceeded=4, max_timeouts=3, request_timeout=60,
                                   encryption_enabled=False, store_sync_tokens=False)
        self.client = AsyncClient(self.homeserver, user_id, device_id=device_id or None, config=config)
        self.client.access_token = token
        self.client.user_id = user_id
        self._upload_limit: int | None = None
        self._limit_known = False

    @staticmethod
    def _check(resp, what: str):
        from nio import ErrorResponse
        if isinstance(resp, ErrorResponse):
            status = getattr(getattr(resp, "transport_response", None), "status", 0) or 0
            raise MxError(str(resp.status_code or "M_UNKNOWN"), str(resp.message or what), status,
                          resp.retry_after_ms)
        return resp

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:
            pass

    async def whoami(self) -> dict:
        r = self._check(await self.client.whoami(), "whoami")
        return {"user_id": r.user_id, "device_id": r.device_id}

    async def _raw(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        """One request through nio's HTTP session, answered as plain JSON (MxError when it is not a 200)."""
        if self.client.client_session is None:      # nio creates its HTTP session on the first typed call
            await self.whoami()
        data = json.dumps(body) if body is not None else None
        resp = await self.client.send(method, path, data=data,
                                      headers={"Content-Type": "application/json"} if data is not None else None,
                                      timeout=timeout)
        try:
            raw = await resp.text()
        finally:
            resp.release()
        try:
            out = json.loads(raw or "{}")
        except ValueError:
            out = {}
        if not isinstance(out, dict):
            out = {}
        if resp.status != 200:
            raise MxError(str(out.get("errcode") or f"HTTP_{resp.status}"), str(out.get("error") or raw[:200]),
                          resp.status, out.get("retry_after_ms"))
        return out

    async def sync(self, since: str | None, timeout_ms: int, sync_filter: dict | None = None) -> dict:
        """One /sync, as the plain JSON the server sent."""
        from nio import Api
        method, path = Api.sync(self.client.access_token, since=since, timeout=timeout_ms or None,
                                filter=sync_filter)
        return await self._raw(method, path, timeout=timeout_ms / 1000 + 30)

    async def send(self, room_id: str, event_type: str, content: dict) -> str:
        r = self._check(await self.client.room_send(room_id, event_type, content, tx_id=txn_id()), "send")
        return r.event_id

    async def redact(self, room_id: str, event_id: str, reason: str | None = None) -> str:
        r = self._check(await self.client.room_redact(room_id, event_id, reason=reason), "redact")
        return r.event_id

    async def typing(self, room_id: str, on: bool = True, timeout_ms: int = 30000) -> None:
        self._check(await self.client.room_typing(room_id, typing_state=on, timeout=timeout_ms), "typing")

    async def join(self, room_id: str) -> str:
        # nio's join() and room_leave() send no request body; the spec's body is a JSON object and stricter
        # servers (continuwuity) refuse an empty one with M_BAD_JSON, so they go out with {} here.
        from nio import Api
        method, path = Api.join(self.client.access_token, room_id)
        return str((await self._raw(method, path, {})).get("room_id") or room_id)

    async def leave(self, room_id: str) -> None:
        from nio import Api
        method, path = Api.room_leave(self.client.access_token, room_id)
        await self._raw(method, path, {})

    async def upload(self, data: bytes, content_type: str, filename: str) -> str:
        resp, _keys = await self.client.upload(lambda *_: data, content_type=content_type, filename=filename,
                                               filesize=len(data))
        return self._check(resp, "upload").content_uri

    async def download(self, mxc: str) -> bytes:
        r = self._check(await self.client.download(mxc=mxc), "download")
        return r.body

    async def upload_limit(self) -> int | None:
        """The server's largest accepted upload in bytes (None: not announced)."""
        if not self._limit_known:
            try:
                r = await self.client.content_repository_config()
                self._upload_limit = getattr(r, "upload_size", None)
            except Exception:
                self._upload_limit = None
            self._limit_known = True
        return self._upload_limit
