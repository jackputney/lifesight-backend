"""ElevenLabs text-to-speech — text in, audio out. No write paths.

Ported from Oliver_Jarvis_V2/app/tts.py. Two changes from the reference:
`synthesize` is async (this backend is async end to end, and TTS sits on the
voice-reply path where a blocking call would stall the event loop), and the
voice id is env-only rather than hardcoded — this repo is PUBLIC, and the
primary user's chosen voice is personal data. Set ELEVENLABS_VOICE_ID in .env.

Stateless: no DB, no user_id, no per-user tokens. Placement in shared/ follows
the shared/google_client.py precedent and is Jack's call at merge — the module
is mode-agnostic, so all three modes can speak in the one Olivia voice.
"""
from __future__ import annotations

import os

import httpx

_API = "https://api.elevenlabs.io/v1"

# Flash v2.5: lowest latency, so read-back and replies start speaking fast.
_DEFAULT_MODEL_ID = "eleven_flash_v2_5"
# mp3_44100_128 is widely available; Creator+ tiers can use higher bitrates.
_DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

# Matches the reference implementation's tuning for the primary user.
_VOICE_SETTINGS = {"stability": 0.45, "similarity_boost": 0.75}

_TIMEOUT_SECONDS = 45.0


class TtsError(Exception):
    """Readable TTS failure surfaced to the caller instead of a raw traceback."""


def _api_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def _voice_id() -> str:
    return os.environ.get("ELEVENLABS_VOICE_ID", "").strip()


def _model_id() -> str:
    return os.environ.get("ELEVENLABS_MODEL_ID", _DEFAULT_MODEL_ID).strip()


def _output_format() -> str:
    return os.environ.get("ELEVENLABS_OUTPUT_FORMAT", _DEFAULT_OUTPUT_FORMAT).strip()


def is_available() -> bool:
    """True when both the key and a voice are configured."""
    return bool(_api_key() and _voice_id())


def status() -> dict:
    """Non-secret config summary. Never returns the API key."""
    available = is_available()
    return {
        "available": available,
        "voice_id": _voice_id() if available else None,
        "model_id": _model_id() if available else None,
        "provider": "elevenlabs" if available else None,
    }


async def synthesize(text: str) -> bytes:
    """Return MP3 bytes for `text`. Raises TtsError on failure."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise TtsError("No text to speak.")
    if not is_available():
        raise TtsError(
            "ElevenLabs is not configured — set ELEVENLABS_API_KEY and "
            "ELEVENLABS_VOICE_ID in .env."
        )

    url = f"{_API}/text-to-speech/{_voice_id()}"
    headers = {
        "xi-api-key": _api_key(),
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    body = {
        "text": cleaned,
        "model_id": _model_id(),
        "voice_settings": _VOICE_SETTINGS,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            res = await client.post(
                url,
                params={"output_format": _output_format()},
                headers=headers,
                json=body,
            )
    except httpx.HTTPError as exc:
        raise TtsError(f"ElevenLabs request failed: {exc}") from exc

    if res.status_code >= 400:
        detail = res.text[:240] if res.text else res.reason_phrase
        raise TtsError(f"ElevenLabs error ({res.status_code}): {detail}")
    if not res.content:
        raise TtsError("ElevenLabs returned empty audio.")
    return res.content
