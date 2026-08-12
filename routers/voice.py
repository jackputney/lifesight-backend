"""Voice synthesis — server-side ElevenLabs TTS for iOS playback.

Apple SpeechRecognizer stays on device. Only TTS goes through this route so
ELEVENLABS_API_KEY never ships in the client.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from shared.auth import get_current_user_id
from shared.elevenlabs_tts import stream_speech_mp3

router = APIRouter(prefix="/voice", tags=["voice"])


class SpeechIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


@router.post("/speech")
async def speech(
    body: SpeechIn,
    user_id: str = Depends(get_current_user_id),
):
    """Synthesize spoken audio for `text`. Returns audio/mpeg (streamed)."""
    _ = user_id  # auth gate only — no user-specific voice routing in V1
    audio = await stream_speech_mp3(body.text)
    return StreamingResponse(
        audio,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
