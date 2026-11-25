"""Files, images and voice in and out of Matrix rooms.

In: `attachment()` reads an m.image / m.file / m.audio / m.video message; `fetch()` downloads it (also an
attachment forwarded out of an encrypted room, which carries its own key, so it is decrypted locally).
Out: `file_content()` builds the m.image (pictures, shown inline) or m.file message for a `[FILE]` marker,
`voice_content()` the m.audio voice message for a `[VOICE]` block.
"""
from __future__ import annotations

import mimetypes
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

MEDIA_TYPES = ("m.image", "m.file", "m.audio", "m.video")
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
IMAGE_MAX = 5 * 1024 * 1024        # larger pictures go to the inbox as files (the model's image limit)
AUDIO_SUFFIX = {"audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
                "audio/x-m4a": ".m4a", "audio/aac": ".m4a", "audio/webm": ".webm", "audio/wav": ".wav",
                "audio/x-wav": ".wav", "audio/flac": ".flac"}


@dataclass
class Attachment:
    kind: str                 # "image" | "file" | "voice" | "audio" | "video"
    url: str                  # mxc:// of the content
    name: str                 # the file name to save it under
    caption: str = ""         # text the owner wrote with it (MSC2530: body differs from filename)
    mimetype: str = ""
    size: int = 0
    encrypted: dict | None = None   # the `file` descriptor (key, iv, hashes) of an encrypted attachment


def attachment(content: dict) -> Attachment | None:
    msgtype = content.get("msgtype")
    if msgtype not in MEDIA_TYPES:
        return None
    enc = content.get("file") if isinstance(content.get("file"), dict) else None
    url = str((enc or {}).get("url") or content.get("url") or "")
    if not url.startswith("mxc://"):
        return None
    info = content.get("info") if isinstance(content.get("info"), dict) else {}
    body = str(content.get("body") or "").strip()
    filename = str(content.get("filename") or "").strip()
    caption = body if filename and body and body != filename else ""
    name = filename or body or msgtype.split(".", 1)[1]
    mimetype = str(info.get("mimetype") or "").lower()
    try:
        size = int(info.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    kind = msgtype.split(".", 1)[1]
    if msgtype == "m.audio" and ("org.matrix.msc3245.voice" in content or "m.voice" in content):
        kind = "voice"
    elif msgtype == "m.image" and (mimetype not in IMAGE_TYPES or size > IMAGE_MAX):
        kind = "file"              # a picture the model cannot take directly (svg, heic, huge): the inbox
    return Attachment(kind, url, name, caption, mimetype, size, enc)


async def fetch(api, att: Attachment) -> bytes:
    data = await api.download(att.url)
    if att.encrypted:
        from nio.crypto.attachments import decrypt_attachment
        try:
            data = decrypt_attachment(data, att.encrypted["key"]["k"], att.encrypted["hashes"]["sha256"],
                                      att.encrypted["iv"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"the attachment could not be decrypted ({e})") from None
    return data


def temp_file(data: bytes, suffix: str) -> Path:
    fd, tmp = tempfile.mkstemp(prefix="archi_mx_", suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return Path(tmp)


def image_suffix(att: Attachment) -> str:
    return IMAGE_TYPES.get(att.mimetype) or (Path(att.name).suffix.lower() or ".jpg")


def audio_suffix(att: Attachment) -> str:
    return AUDIO_SUFFIX.get(att.mimetype.split(";")[0]) or (Path(att.name).suffix.lower() or ".ogg")


# ------------------------------------------------------------------------------------------- out

def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a png/gif/jpeg/webp from its header (stdlib only). Element lays the timeline out from
    info.w/h; without them the picture jumps when it loads."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return (w & 0x3FFF, h & 0x3FFF)
            if kind == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return ((b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1)
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                m = data[i + 1]
                if m == 0xFF:
                    i += 1
                    continue
                if m in (0x01, 0xD8) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):   # start of frame
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return (w, h)
                i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    except (struct.error, IndexError):
        pass
    return None


def file_content(name: str, data: bytes) -> tuple[dict, str]:
    """-> (message content without its url, content type). Pictures whose size can be read become m.image
    (shown inline), everything else m.file (a download)."""
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    size = image_size(data) if mime in IMAGE_TYPES else None
    info: dict = {"mimetype": mime, "size": len(data)}
    if size:
        info["w"], info["h"] = size
        return {"msgtype": "m.image", "body": name, "filename": name, "info": info}, mime
    return {"msgtype": "m.file", "body": name, "filename": name, "info": info}, mime


def voice_content(name: str, size: int, duration_ms: int) -> dict:
    """An m.audio voice message (MSC3245): Element shows it as a voice note with a play button."""
    return {"msgtype": "m.audio", "body": "Voice message", "filename": name,
            "info": {"mimetype": "audio/ogg", "size": size, "duration": duration_ms},
            "org.matrix.msc1767.audio": {"duration": duration_ms, "waveform": [256] * 30},
            "org.matrix.msc3245.voice": {}}
