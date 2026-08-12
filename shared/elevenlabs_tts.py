"""Server-side ElevenLabs TTS — keys never leave the backend.

Streams MP3 via POST /v1/text-to-speech/{voice_id}/stream (Flash v2.5 by default).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException

ELEVENLABS_STREAM_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
DEFAULT_MODEL_ID = "eleven_flash_v2_5"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
MAX_SPEECH_CHARS = 5000


def _api_key() -> str:
    key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not key:
        raise HTTPException(
            status_code=503, detail="ELEVENLABS_API_KEY is not configured"
        )
    return key


def _voice_id() -> str:
    voice_id = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    if not voice_id:
        raise HTTPException(
            status_code=503, detail="ELEVENLABS_VOICE_ID is not configured"
        )
    return voice_id


def _model_id() -> str:
    return (os.environ.get("ELEVENLABS_MODEL_ID") or DEFAULT_MODEL_ID).strip()


def normalize_speech_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="text must be non-empty")
    if len(value) > MAX_SPEECH_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"text must be at most {MAX_SPEECH_CHARS} characters",
        )
    return value


async def stream_speech_mp3(text: str) -> AsyncIterator[bytes]:
    """Yield MP3 bytes from ElevenLabs streaming TTS. Caller owns consumption."""
    text = normalize_speech_text(text)
    api_key = _api_key()
    voice_id = _voice_id()
    model_id = _model_id()
    url = ELEVENLABS_STREAM_URL.format(voice_id=voice_id)
    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {"text": text, "model_id": model_id}
    params = {"output_format": DEFAULT_OUTPUT_FORMAT}

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    request = client.build_request(
        "POST", url, headers=headers, params=params, json=payload
    )
    try:
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=502, detail="ElevenLabs TTS request failed"
        ) from exc

    if response.status_code != 200:
        try:
            await response.aread()
        except Exception:
            pass
        await response.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail="ElevenLabs TTS unavailable",
        )

    async def _chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return _chunks()
