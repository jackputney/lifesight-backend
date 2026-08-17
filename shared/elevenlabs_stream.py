"""ElevenLabs stream-input websocket — one session per assistant turn.

The API has no cancel message, so barge-in means closing that turn's socket
(`abort`). The default chunk schedule buffers ~120 characters before emitting
any audio, so a short reply produces nothing until `flush_and_finish` — always
finish a turn. Auth is the `xi-api-key` handshake header, never an init field
(the docs disagree with themselves on the init field's name).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any, Callable, Optional

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

# Reused deliberately: one definition of "ElevenLabs is configured" (503 when
# not) shared with the REST POST /voice/speech path.
from shared.elevenlabs_tts import (
    DEFAULT_OUTPUT_FORMAT,
    _api_key,
    _model_id,
    _voice_id,
)

STREAM_INPUT_URL = (
    "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
)

# Seconds ElevenLabs holds the socket open with no input. Must comfortably
# outlast a tool round inside one assistant turn.
INACTIVITY_TIMEOUT_SECONDS = 180

# ElevenLabs' documented default schedule. Earlier first chunk = lower latency,
# slightly worse prosody; 120 is their recommended starting point.
CHUNK_LENGTH_SCHEDULE: tuple[int, ...] = (120, 160, 250, 290)

DEFAULT_VOICE_SETTINGS: dict[str, Any] = {
    "stability": 0.5,
    "similarity_boost": 0.8,
}

CONNECT_TIMEOUT_SECONDS = 10


class ElevenLabsStreamError(RuntimeError):
    """Upstream TTS failure. Never fatal to the text half of a turn."""


def stream_input_url(*, voice_id: str, model_id: str, output_format: str) -> str:
    return (
        STREAM_INPUT_URL.format(voice_id=voice_id)
        + f"?model_id={model_id}"
        + f"&output_format={output_format}"
        + f"&inactivity_timeout={INACTIVITY_TIMEOUT_SECONDS}"
    )


class ElevenLabsStreamSession:
    """Async streaming TTS session for exactly one assistant turn.

    Usage:
        async with ElevenLabsStreamSession() as session:
            await session.send_text("Hello there ")
            async for audio in session.audio_chunks():
                ...
    """

    def __init__(
        self,
        *,
        voice_id: Optional[str] = None,
        model_id: Optional[str] = None,
        api_key: Optional[str] = None,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
        connect: Optional[Callable[..., Any]] = None,
    ):
        self._voice_id = voice_id
        self._model_id = model_id
        self._api_key = api_key
        self._output_format = output_format
        self._connect = connect or websocket_connect
        self._ws: Any = None
        self._aborted = False
        self._finished = False
        self._closed = False

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "ElevenLabsStreamSession":
        await self.open()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def open(self) -> None:
        """Connect and send the init message. Raises ElevenLabsStreamError.

        Missing ELEVENLABS_* configuration raises HTTPException(503) from the
        shared accessors, matching POST /voice/speech.
        """
        if self._ws is not None:
            return
        api_key = self._api_key or _api_key()
        voice_id = self._voice_id or _voice_id()
        model_id = self._model_id or _model_id()
        url = stream_input_url(
            voice_id=voice_id,
            model_id=model_id,
            output_format=self._output_format,
        )
        try:
            self._ws = await self._connect(
                url,
                additional_headers={"xi-api-key": api_key},
                open_timeout=CONNECT_TIMEOUT_SECONDS,
                max_size=None,
            )
        except (WebSocketException, OSError, TimeoutError) as exc:
            self._ws = None
            raise ElevenLabsStreamError("ElevenLabs streaming connect failed") from exc

        init = {
            "text": " ",
            "voice_settings": dict(DEFAULT_VOICE_SETTINGS),
            "generation_config": {
                "chunk_length_schedule": list(CHUNK_LENGTH_SCHEDULE)
            },
        }
        await self._send_json(init)

    async def close(self) -> None:
        """Close the upstream socket. Idempotent; safe on every exit path."""
        ws, self._ws = self._ws, None
        self._closed = True
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            # The turn is over either way; a failed close must not propagate.
            pass

    async def abort(self) -> None:
        """Barge-in: drop this turn's audio immediately by closing the socket."""
        self._aborted = True
        await self.close()

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def closed(self) -> bool:
        return self._closed

    # -- input --------------------------------------------------------------

    async def _send_json(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ElevenLabsStreamError("ElevenLabs session is not open")
        try:
            await ws.send(json.dumps(payload))
        except (WebSocketException, OSError) as exc:
            raise ElevenLabsStreamError("ElevenLabs streaming send failed") from exc

    async def send_text(self, text: str) -> None:
        """Feed one incremental chunk. Forced to end in a space per protocol."""
        if self._aborted or self._finished or not text.strip():
            return
        await self._send_json({"text": _space_terminated(text)})

    async def flush(self, trailing_text: str = "") -> None:
        """Force generation of buffered text without ending the turn.

        Used at tool boundaries so speech already produced isn't stuck behind
        the 120-character chunk schedule while a tool runs.
        """
        if self._aborted or self._finished:
            return
        await self._send_json({"text": _space_terminated(trailing_text), "flush": True})

    async def flush_and_finish(self, trailing_text: str = "") -> None:
        """Force generation of everything buffered, then end the turn."""
        if self._aborted or self._finished:
            return
        await self.flush(trailing_text)
        self._finished = True
        await self._send_json({"text": ""})

    # -- output -------------------------------------------------------------

    async def audio_chunks(self) -> AsyncIterator[bytes]:
        """Yield decoded MP3 bytes until the turn's final frame or an abort."""
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                if self._aborted:
                    return
                payload = _decode_frame(raw)
                if payload is None:
                    continue
                error = payload.get("error") or payload.get("message")
                if error and payload.get("audio") is None and not _is_final(payload):
                    raise ElevenLabsStreamError("ElevenLabs streaming error")
                audio = payload.get("audio")
                if audio:
                    yield base64.b64decode(audio)
                # Both spellings appear in ElevenLabs docs; missing this hangs
                # the turn until the inactivity timeout.
                if _is_final(payload):
                    return
        except ConnectionClosed:
            if self._aborted:
                return
            raise ElevenLabsStreamError("ElevenLabs stream closed unexpectedly") from None
        except (WebSocketException, OSError) as exc:
            if self._aborted:
                return
            raise ElevenLabsStreamError("ElevenLabs stream failed") from exc


def _space_terminated(text: str) -> str:
    """stream-input treats the trailing space as the word boundary."""
    value = text if (text or "").strip() else " "
    return value if value.endswith(" ") else value + " "


def _decode_frame(raw: Any) -> Optional[dict[str, Any]]:
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_final(payload: dict[str, Any]) -> bool:
    return bool(payload.get("isFinal") or payload.get("is_final"))
