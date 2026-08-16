"""Cached stalling audio for tool latency — closed allowlist, never user text.

Only the four phrases in STALL_PHRASES can ever be synthesized or cached, so
private/user speech can never reach the cache directory. A phrase is truthful
by construction: it is emitted only from stall_key_for_tool(<tool actually
being invoked>), and no "almost done" style fake-progress phrase exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

from shared.elevenlabs_tts import _voice_id, stream_speech_mp3

# Bump when any phrase text changes so stale audio is never replayed.
STALL_AUDIO_CACHE_VERSION = "v1"

CACHE_DIR_ENV = "STALL_AUDIO_CACHE_DIR"
DEFAULT_CACHE_DIR_NAME = "lifesight-stall-audio"

STALL_KEY_CALENDAR_CHECK = "calendar_check"
STALL_KEY_HEALTH_CHECK = "health_check"
STALL_KEY_MAIL_CHECK = "mail_check"
STALL_KEY_GENERAL_TOOL_CHECK = "general_tool_check"

# The complete allowlist. Anything else raises — this mapping is the security
# boundary between "spoken filler" and "arbitrary text sent to a vendor".
STALL_PHRASES: Mapping[str, str] = MappingProxyType(
    {
        STALL_KEY_CALENDAR_CHECK: "Let me check your calendar.",
        STALL_KEY_HEALTH_CHECK: "I'm checking your latest health data.",
        STALL_KEY_MAIL_CHECK: "Let me look at your mail.",
        STALL_KEY_GENERAL_TOOL_CHECK: "One moment while I pull that up.",
    }
)

# Tool name → stall key. Truthfulness: the phrase must describe the work that
# is actually starting, so this maps real tool names only.
_TOOL_STALL_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "list_calendar_events": STALL_KEY_CALENDAR_CHECK,
        "get_recent_health_data": STALL_KEY_HEALTH_CHECK,
        "send_email": STALL_KEY_MAIL_CHECK,
        "search_email": STALL_KEY_MAIL_CHECK,
        "list_email": STALL_KEY_MAIL_CHECK,
        "read_email": STALL_KEY_MAIL_CHECK,
    }
)

_MAIL_TOOL_PREFIXES: tuple[str, ...] = ("gmail_", "mail_", "email_")

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_-]")

# One synthesis per key per process; concurrent turns share the result instead
# of racing on the same cache file.
_locks: dict[str, asyncio.Lock] = {}


class UnknownStallKeyError(ValueError):
    """Raised for any key outside STALL_PHRASES — including arbitrary text."""


class StallAudioError(RuntimeError):
    """Stall audio could not be produced. Always non-fatal to the turn."""


def stall_keys() -> frozenset[str]:
    return frozenset(STALL_PHRASES)


def stall_phrase(stall_key: str) -> str:
    """Exact phrase for an allowlisted key. Raises UnknownStallKeyError."""
    try:
        return STALL_PHRASES[stall_key]
    except (KeyError, TypeError) as exc:
        raise UnknownStallKeyError("stall_key is not allowlisted") from exc


def stall_key_for_tool(tool_name: str) -> str:
    """Map the tool actually being invoked to a truthful stall phrase key."""
    name = (tool_name or "").strip().lower()
    mapped = _TOOL_STALL_KEYS.get(name)
    if mapped is not None:
        return mapped
    if name.startswith(_MAIL_TOOL_PREFIXES):
        return STALL_KEY_MAIL_CHECK
    return STALL_KEY_GENERAL_TOOL_CHECK


def cache_root() -> Path:
    configured = (os.environ.get(CACHE_DIR_ENV) or "").strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / DEFAULT_CACHE_DIR_NAME


def _safe_segment(value: str) -> str:
    cleaned = _SAFE_SEGMENT.sub("", (value or "").strip())
    if cleaned:
        return cleaned[:64]
    # Voice ids are opaque vendor strings; hash anything unusable as a path.
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def cache_path(stall_key: str, *, voice_id: str) -> Path:
    """Version-keyed on-disk location. Validates the key first."""
    stall_phrase(stall_key)
    return (
        cache_root()
        / STALL_AUDIO_CACHE_VERSION
        / _safe_segment(voice_id)
        / f"{stall_key}.mp3"
    )


def _write_atomic(path: Path, data: bytes) -> None:
    """Temp file + os.replace so concurrent writers can't produce a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".part")
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


async def _synthesize(phrase: str) -> bytes:
    try:
        chunks = await stream_speech_mp3(phrase)
        audio = b"".join([chunk async for chunk in chunks])
    except Exception as exc:
        raise StallAudioError("stall audio synthesis failed") from exc
    if not audio:
        raise StallAudioError("stall audio synthesis returned no audio")
    return audio


async def get_stall_audio(stall_key: str, *, voice_id: Optional[str] = None) -> bytes:
    """MP3 bytes for an allowlisted stall phrase, synthesizing once per key.

    Raises UnknownStallKeyError for anything outside the allowlist (checked
    before any filesystem or vendor call) and StallAudioError on TTS failure.
    """
    phrase = stall_phrase(stall_key)
    resolved_voice = voice_id or _voice_id()
    path = cache_path(stall_key, voice_id=resolved_voice)

    cached = _read_cached(path)
    if cached is not None:
        return cached

    lock = _locks.setdefault(str(path), asyncio.Lock())
    async with lock:
        cached = _read_cached(path)
        if cached is not None:
            return cached
        audio = await _synthesize(phrase)
        try:
            _write_atomic(path, audio)
        except OSError:
            # An unwritable cache is a performance problem, not a failure.
            pass
        return audio


def _read_cached(path: Path) -> Optional[bytes]:
    try:
        data = path.read_bytes()
    except (OSError, ValueError):
        return None
    return data or None
