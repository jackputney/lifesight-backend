"""Wire frames for WS /chat/stream — mirrored in docs/STREAMING_VOICE_V1_CONTRACT.md.

Every string constant here (frame types, audio kinds, error codes) is part of the
frozen iOS contract; changing one is a cross-repo break, not a local refactor.
"""

from __future__ import annotations

import base64
import json
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

# --- frame type strings (server → client unless noted) ----------------------
FRAME_USER_TURN = "user_turn"            # client → server
FRAME_INTERRUPT = "interrupt"            # client → server
FRAME_TURN_STARTED = "turn_started"
FRAME_TEXT_DELTA = "text_delta"
FRAME_AUDIO_CHUNK = "audio_chunk"
FRAME_TURN_CANCELLED = "turn_cancelled"
FRAME_ERROR = "error"
FRAME_RESPONSE_COMPLETE = "response_complete"

# --- audio_chunk.kind -------------------------------------------------------
AUDIO_KIND_ASSISTANT = "assistant"
AUDIO_KIND_STALL = "stall"
AUDIO_MIME_TYPE = "audio/mpeg"

# --- error.code (stable enum) ----------------------------------------------
ERROR_INVALID_FRAME = "invalid_frame"
ERROR_UNSUPPORTED_MODE = "unsupported_mode"
ERROR_INVALID_CONVERSATION = "invalid_conversation"
ERROR_FORBIDDEN_CONVERSATION = "forbidden_conversation"
ERROR_MODEL_UNAVAILABLE = "model_unavailable"
ERROR_MODEL_ERROR = "model_error"
ERROR_TTS_UNAVAILABLE = "tts_unavailable"
ERROR_TTS_ERROR = "tts_error"
ERROR_INTERNAL = "internal_error"

ERROR_CODES: frozenset[str] = frozenset(
    {
        ERROR_INVALID_FRAME,
        ERROR_UNSUPPORTED_MODE,
        ERROR_INVALID_CONVERSATION,
        ERROR_FORBIDDEN_CONVERSATION,
        ERROR_MODEL_UNAVAILABLE,
        ERROR_MODEL_ERROR,
        ERROR_TTS_UNAVAILABLE,
        ERROR_TTS_ERROR,
        ERROR_INTERNAL,
    }
)

# Transport-level bound on a single spoken turn. REST /chat has no explicit cap
# because FastAPI bounds the body; a raw socket needs its own guard.
MAX_MESSAGE_CHARS = 16_000

# Bound on the whole JSON envelope, checked before it is parsed — the
# MAX_MESSAGE_CHARS field bound only applies after json.loads has already
# walked the payload. Generous headroom over MAX_MESSAGE_CHARS because JSON
# escaping can expand a legitimate message several times over.
MAX_FRAME_CHARS = 4 * MAX_MESSAGE_CHARS


class VoiceOptions(BaseModel):
    """Per-turn voice toggle. Absent object means voice on."""

    enabled: bool = True


class UserTurnFrame(BaseModel):
    type: Literal["user_turn"]
    mode: str = "fitness"
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)
    conversation_id: Optional[str] = None
    voice: VoiceOptions = Field(default_factory=VoiceOptions)


class InterruptFrame(BaseModel):
    type: Literal["interrupt"]
    turn_id: str = Field(..., min_length=1, max_length=64)


ClientFrame = Annotated[
    Union[UserTurnFrame, InterruptFrame], Field(discriminator="type")
]

_CLIENT_FRAME_ADAPTER: TypeAdapter = TypeAdapter(ClientFrame)


class ProtocolError(ValueError):
    """Malformed client frame — always answered with an `error` frame."""

    def __init__(self, message: str, code: str = ERROR_INVALID_FRAME):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_client_frame(raw: str | bytes | None) -> UserTurnFrame | InterruptFrame:
    """Decode one client frame. Never raises anything but ProtocolError."""
    if raw is None:
        raise ProtocolError("Expected a JSON text frame")
    if len(raw) > MAX_FRAME_CHARS:
        # Rejected before decoding or parsing: nothing this large is a valid
        # frame, and json.loads on it is wasted work on a raw socket.
        raise ProtocolError(f"Frame exceeds {MAX_FRAME_CHARS} characters")
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("Frame must be UTF-8 JSON") from exc
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("Frame must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Frame must be a JSON object")
    try:
        return _CLIENT_FRAME_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        # Never echo the raw payload back — it can contain the user's speech.
        raise ProtocolError(_first_validation_reason(exc)) from exc


def _first_validation_reason(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Unsupported frame"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "function-after")
    reason = str(first.get("msg") or "invalid value")
    return f"{location or 'type'}: {reason}"


# ---------------------------------------------------------------------------
# Server → client builders. Plain dicts so the send choke-point can inspect
# `type` without importing model classes.
# ---------------------------------------------------------------------------

def turn_started_frame(*, turn_id: str, conversation_id: str) -> dict[str, Any]:
    return {
        "type": FRAME_TURN_STARTED,
        "turn_id": turn_id,
        "conversation_id": conversation_id,
    }


def text_delta_frame(*, turn_id: str, delta: str) -> dict[str, Any]:
    return {"type": FRAME_TEXT_DELTA, "turn_id": turn_id, "delta": delta}


def audio_chunk_frame(
    *, turn_id: str, sequence: int, kind: str, audio: bytes
) -> dict[str, Any]:
    if kind not in (AUDIO_KIND_ASSISTANT, AUDIO_KIND_STALL):
        raise ValueError(f"unsupported audio kind: {kind}")
    return {
        "type": FRAME_AUDIO_CHUNK,
        "turn_id": turn_id,
        "sequence": sequence,
        "kind": kind,
        "mime_type": AUDIO_MIME_TYPE,
        "data_base64": base64.b64encode(audio).decode("ascii"),
    }


def turn_cancelled_frame(*, turn_id: str) -> dict[str, Any]:
    return {"type": FRAME_TURN_CANCELLED, "turn_id": turn_id}


def error_frame(*, turn_id: str | None, code: str, message: str) -> dict[str, Any]:
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code: {code}")
    return {
        "type": FRAME_ERROR,
        "turn_id": turn_id,
        "code": code,
        "message": message,
    }


def response_complete_frame(
    *,
    turn_id: str,
    conversation_id: str,
    reply: str,
    pending_action: dict | None = None,
    visual_panel: dict | None = None,
    research: dict | None = None,
    client_actions: list[dict] | None = None,
) -> dict[str, Any]:
    """Terminal frame — same field semantics as the REST ChatResponse body."""
    return {
        "type": FRAME_RESPONSE_COMPLETE,
        "turn_id": turn_id,
        "conversation_id": conversation_id,
        "reply": reply,
        "pending_action": pending_action,
        "visual_panel": visual_panel,
        "research": research,
        "client_actions": list(client_actions or []),
    }
