"""Thin Bot API layer: one `call(method, **params)` that returns plain JSON-like data.

* `TgApi` (async, used by the door) goes through aiogram's Bot for every method aiogram knows, and falls
  back to a raw HTTPS call for methods it does not know yet (the Bot API moves faster than libraries).
* `SyncApi` (stdlib only, used by the `archi` CLI that the agent runs during a turn) is a raw client.

Both raise TgError with Telegram's description, so callers can recognise e.g. a deleted topic.
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API_URL = "https://api.telegram.org"
_TOPIC_GONE = re.compile(r"TOPIC_ID_INVALID|TOPIC_DELETED|message thread not found|thread not found", re.I)
_CODES = {"TelegramBadRequest": 400, "TelegramUnauthorizedError": 401, "TelegramForbiddenError": 403,
          "TelegramNotFound": 404, "TelegramConflictError": 409, "TelegramEntityTooLarge": 413,
          "TelegramRetryAfter": 429, "TelegramServerError": 500}


class TgError(Exception):
    def __init__(self, description: str, code: int = 0, method: str = "", retry_after: float | None = None):
        super().__init__(description)
        self.description = description or ""
        self.code = code
        self.method = method
        self.retry_after = retry_after

    @property
    def topic_gone(self) -> bool:
        """The topic (message thread) no longer exists."""
        return bool(_TOPIC_GONE.search(self.description))

    @property
    def not_modified(self) -> bool:
        return "NOT_MODIFIED" in self.description.upper() or "not modified" in self.description

    @property
    def topic_closed(self) -> bool:
        return "TOPIC_CLOSED" in self.description.upper()

    @property
    def bad_markup(self) -> bool:
        return "can't parse entities" in self.description or "unsupported start tag" in self.description

    def __str__(self) -> str:
        return f"{self.method or 'call'}: {self.description}"


def snake(method: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", method).lower()


def plain(value: Any) -> Any:
    """aiogram objects -> the JSON shape the Bot API documents."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True, by_alias=True)
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


class TgApi:
    """Async Bot API access for the door."""

    def __init__(self, token: str, bot=None):
        from aiogram import Bot   # imported here so the CLI and the tests never need aiogram
        self.token = token
        self.bot = bot or Bot(token=token)
        self._raw_session = None

    async def close(self) -> None:
        try:
            await self.bot.session.close()
        except Exception:
            pass
        if self._raw_session is not None:
            await self._raw_session.close()

    async def call(self, method: str, **params) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(2):
            try:
                return await self._call_once(method, params)
            except TgError as e:
                if e.retry_after and attempt == 0 and e.retry_after <= 30:
                    await asyncio.sleep(e.retry_after + 0.5)
                    continue
                raise
        raise TgError("retry failed", method=method)   # unreachable

    async def _call_once(self, method: str, params: dict) -> Any:
        from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
        from aiogram.types import FSInputFile
        fn = getattr(self.bot, snake(method), None)
        if fn is None or not callable(fn):
            return await self._raw(method, params)
        for k, v in list(params.items()):
            if isinstance(v, Path):
                params[k] = FSInputFile(v)
        try:
            return plain(await fn(**params))
        except TelegramRetryAfter as e:
            raise TgError(e.message, 429, method, retry_after=float(e.retry_after)) from None
        except TelegramNetworkError as e:
            raise TgError(f"network error: {e.message}", 0, method) from None
        except TelegramAPIError as e:
            raise TgError(e.message, _CODES.get(type(e).__name__, 400), method) from None

    async def _raw(self, method: str, params: dict) -> Any:
        import aiohttp
        if self._raw_session is None:
            self._raw_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        try:
            async with self._raw_session.post(f"{API_URL}/bot{self.token}/{method}", json=params) as r:
                body = await r.json(content_type=None)
        except aiohttp.ClientError as e:
            raise TgError(f"network error: {e}", 0, method) from None
        return _unwrap(body, method)

    async def download(self, file_id: str, dest: Path) -> Path:
        from aiogram.exceptions import TelegramAPIError
        try:
            await self.bot.download(file_id, destination=dest)
        except TelegramAPIError as e:
            raise TgError(e.message, 400, "getFile") from None
        return dest


def _unwrap(body: Any, method: str) -> Any:
    if not isinstance(body, dict):
        raise TgError(f"unexpected answer: {str(body)[:200]}", 0, method)
    if body.get("ok"):
        return body.get("result")
    params = body.get("parameters") or {}
    raise TgError(str(body.get("description") or "error"), int(body.get("error_code") or 0), method,
                  retry_after=params.get("retry_after"))


class SyncApi:
    """Minimal stdlib Bot API client (the `archi` CLI must work without the venv)."""

    def __init__(self, token: str, timeout: float = 30):
        self.token = token
        self.timeout = timeout

    def call(self, method: str, **params) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        req = urllib.request.Request(f"{API_URL}/bot{self.token}/{method}",
                                     data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except ValueError:
                raise TgError(f"HTTP {e.code}", e.code, method) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TgError(f"network error: {e}", 0, method) from None
        return _unwrap(body, method)
