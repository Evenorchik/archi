"""Speech to text (and text to speech for [VOICE] answers) through the OpenAI audio API.

Optional: without OPENAI_API_KEY `available()` is False and the doors ask the owner for a key.

gpt-4o(-mini)-transcribe stops at 2048 output tokens and returns what it has WITHOUT an error, so a long
voice note comes back silently cut. Notes longer than a few minutes are therefore cut at pauses (ffmpeg
silencedetect) into ~3-minute pieces, transcribed piece by piece and joined. Short notes go in one request.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
from pathlib import Path

from .config import Config

API = "https://api.openai.com/v1"
SINGLE_MAX_S = 200      # up to this length: one request
CHUNK_S = 180           # target piece length for long notes
CAP_TOKENS = 2000       # output_tokens at or above this = the model probably hit its ceiling
CUT_WARNING = ("\n\n[Note: the transcription probably stopped early; the end of the voice message may "
               "be missing.]")
_KEY_RE = re.compile(r"^sk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}$")


def looks_like_openai_key(text: str) -> bool:
    return bool(_KEY_RE.match((text or "").strip()))


def available(cfg: Config | None = None) -> bool:
    return bool((cfg or Config()).get("OPENAI_API_KEY"))


async def _run(*cmd: str, timeout: float = 120) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE,
                                             stdin=asyncio.subprocess.DEVNULL)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, "", "timeout"
    return p.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def duration_s(path: str) -> float:
    if not shutil.which("ffprobe"):
        return 0.0
    rc, out, _ = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path, timeout=30)
    try:
        return float(out.strip().splitlines()[0]) if rc == 0 else 0.0
    except (ValueError, IndexError):
        return 0.0


async def pauses(path: str) -> list[float]:
    """Midpoints of pauses (>= 0.35 s below -35 dB): cutting there does not split a word."""
    if not shutil.which("ffmpeg"):
        return []
    _rc, _out, err = await _run("ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af",
                                "silencedetect=noise=-35dB:d=0.35", "-f", "null", "-", timeout=300)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return [(s + e) / 2 for s, e in zip(starts, ends)]


def cut_points(total: float, pause_marks: list[float], chunk_s: float = CHUNK_S) -> list[float]:
    """Cut positions every ~chunk_s seconds, moved to the nearest pause within 40 s."""
    cuts, last = [], 0.0
    while total - last > chunk_s + 30:          # +30: never leave a tiny tail piece
        target = last + chunk_s
        near = [p for p in pause_marks if target - 40 <= p <= target + 40 and p > last + 1]
        cut = min(near, key=lambda p: abs(p - target)) if near else target
        cuts.append(cut)
        last = cut
    return cuts


async def split(path: str, total: float, workdir: Path) -> list[Path]:
    bounds = [0.0, *cut_points(total, await pauses(path)), total]
    parts = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        out = workdir / f"part{i:02d}.ogg"
        rc, _o, _e = await _run("ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}",
                                "-i", path, "-c:a", "libopus", "-b:a", "32k", str(out), timeout=300)
        if rc != 0 or not out.exists():
            return []
        parts.append(out)
    return parts


async def _transcribe_one(path: str, *, key: str, model: str, language: str | None,
                          prompt: str | None) -> tuple[str, bool]:
    import aiohttp
    form = aiohttp.FormData()
    form.add_field("model", model)
    form.add_field("response_format", "json")
    if language:
        form.add_field("language", language)
    if prompt:
        form.add_field("prompt", prompt)
    with open(path, "rb") as fh:
        form.add_field("file", fh, filename=Path(path).name, content_type="audio/ogg")
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{API}/audio/transcriptions", data=form,
                              headers={"Authorization": f"Bearer {key}"}) as r:
                body = await r.json(content_type=None)
                if r.status != 200:
                    msg = (body.get("error") or {}).get("message") if isinstance(body, dict) else body
                    raise RuntimeError(f"transcription failed ({r.status}): {msg}")
    used = ((body.get("usage") or {}).get("output_tokens") or 0) if isinstance(body, dict) else 0
    return str(body.get("text") or "").strip(), used >= CAP_TOKENS


async def transcribe(path: str, cfg: Config | None = None, *, language: str | None = None) -> str:
    """Voice note -> text. Long notes are split at pauses so nothing is silently cut off."""
    cfg = cfg or Config()
    key = cfg.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("no OPENAI_API_KEY configured")
    model = cfg.get("VOICE_MODEL") or "gpt-4o-mini-transcribe"
    kw = dict(key=key, model=model, language=language or cfg.get("VOICE_LANGUAGE") or None,
              prompt=cfg.get("VOICE_PROMPT") or None)
    total = await duration_s(path)
    if 0 < total <= SINGLE_MAX_S or not shutil.which("ffmpeg"):
        text, capped = await _transcribe_one(path, **kw)
        return text + (CUT_WARNING if capped and total > SINGLE_MAX_S else "")
    with tempfile.TemporaryDirectory(prefix="archi_stt_") as d:
        parts = await split(path, total, Path(d)) if total > 0 else []
        if not parts:
            text, capped = await _transcribe_one(path, **kw)
            return text + (CUT_WARNING if capped else "")
        results = await asyncio.gather(*(_transcribe_one(str(p), **kw) for p in parts))
    text = " ".join(t for t, _c in results if t).strip()
    return text + (CUT_WARNING if any(c for _t, c in results) else "")


async def synthesize(text: str, cfg: Config | None = None, *, speed: float = 1.0) -> Path | None:
    """Text -> OGG/Opus voice note. None when there is no key or the request failed."""
    import aiohttp
    cfg = cfg or Config()
    key = cfg.get("OPENAI_API_KEY")
    if not key:
        return None
    out = Path(tempfile.gettempdir()) / f"archi_tts_{int(time.time() * 1000)}.ogg"
    payload = {"model": cfg.get("TTS_MODEL") or "gpt-4o-mini-tts", "voice": cfg.get("TTS_VOICE") or "alloy",
               "input": text[:4000], "response_format": "opus", "speed": max(0.25, min(4.0, speed))}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
            async with s.post(f"{API}/audio/speech", json=payload,
                              headers={"Authorization": f"Bearer {key}"}) as r:
                if r.status != 200:
                    return None
                out.write_bytes(await r.read())
        return out
    except Exception:
        return None


async def check_key(key: str) -> bool | None:
    """True = the key works, False = rejected (401), None = could not check (network, other errors)."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(f"{API}/models", headers={"Authorization": f"Bearer {key}"}) as r:
                if r.status == 200:
                    return True
                if r.status in (401, 403):
                    return False
                return None
    except Exception:
        return None
