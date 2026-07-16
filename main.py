"""Voice Companion backend — mode router + identity/devices.

POST /chat routes {transcript, mode} to the matching mode system prompt via
MODE_REGISTRY and calls Claude. Identity now comes from the auth layer
(Depends(get_current_user_id)) — the client no longer asserts its own user_id.

/me and /devices provide identity plus push-target registration. Auth is
stubbed in shared/auth.py (AUTH_MODE=dev by default); swapping to real Supabase
JWT verification touches only that file.
"""
import os
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

import anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from modes.author.prompt import SYSTEM_PROMPT as AUTHOR_PROMPT
from modes.health.prompt import SYSTEM_PROMPT as HEALTH_PROMPT
from modes.jarvis.prompt import SYSTEM_PROMPT as JARVIS_PROMPT
from shared.auth import get_current_user_id

load_dotenv()

app = FastAPI(title="Lifesight Backend")
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

MODE_REGISTRY = {
    "author": AUTHOR_PROMPT,
    "health": HEALTH_PROMPT,
    "jarvis": JARVIS_PROMPT,
}


class ChatRequest(BaseModel):
    transcript: str = Field(..., min_length=1)
    mode: str = "author"
    # NOTE: user_id was removed from the request body — identity comes from the
    # auth token via Depends(get_current_user_id), so a client can no longer
    # claim to be another user.


class ChatResponse(BaseModel):
    response: str
    mode: str


class DeviceRegister(BaseModel):
    device_id: str            # client-generated stable ID (e.g. iOS identifierForVendor)
    push_token: Optional[str] = None
    platform: str = "ios"     # ios | android | web


class DeviceOut(BaseModel):
    device_id: str
    user_id: UUID
    push_token: Optional[str]
    platform: str
    last_seen: datetime


def _build_system_prompt(mode: str) -> str:
    today = date.today().isoformat()
    now_local = datetime.now().strftime("%A %B %d, %Y at %I:%M %p").replace(" 0", " ")
    return (
        f"{MODE_REGISTRY[mode]}\n\n"
        f"Today's date is {today}. Current local time: {now_local}."
    )


# ---------------------------------------------------------------------------
# Health / identity
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/modes")
def modes():
    return {"modes": sorted(MODE_REGISTRY)}


@app.get("/me")
async def me(user_id: str = Depends(get_current_user_id)):
    """Resolved user identity. In dev mode this is always the fixed dev UUID,
    which proves the auth plumbing works end to end."""
    return {"user_id": user_id}


# ---------------------------------------------------------------------------
# Chat (mode router)
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user_id: str = Depends(get_current_user_id)):
    mode = req.mode.lower().strip()
    if mode not in MODE_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported mode '{mode}'. Valid modes: {sorted(MODE_REGISTRY)}",
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_build_system_prompt(mode),
        messages=[{"role": "user", "content": req.transcript}],
    )

    text_blocks = [block.text for block in message.content if block.type == "text"]
    if not text_blocks:
        raise HTTPException(status_code=502, detail="Model returned no text")

    return ChatResponse(response=text_blocks[0].strip(), mode=mode)


# ---------------------------------------------------------------------------
# Devices (push-notification targets per user)
# ---------------------------------------------------------------------------
# In-memory stub so the API runs today. Swap _devices for real DB calls
# (asyncpg) once the database is up — route signatures and auth wiring stay
# identical. Backed by migrations/001_users_devices.sql.

_devices: dict[tuple[str, str], dict] = {}  # (user_id, device_id) -> row


@app.post("/devices", response_model=DeviceOut)
async def register_device(
    body: DeviceRegister,
    user_id: str = Depends(get_current_user_id),
):
    """Upsert a device for the current user. Called by the mobile app on launch
    and whenever the push token rotates."""
    row = {
        "device_id": body.device_id,
        "user_id": user_id,
        "push_token": body.push_token,
        "platform": body.platform,
        "last_seen": datetime.now(timezone.utc),
    }
    _devices[(user_id, body.device_id)] = row
    return row


@app.get("/devices", response_model=list[DeviceOut])
async def list_devices(user_id: str = Depends(get_current_user_id)):
    return [r for (uid, _), r in _devices.items() if uid == user_id]


@app.delete("/devices/{device_id}", status_code=204)
async def remove_device(
    device_id: str,
    user_id: str = Depends(get_current_user_id),
):
    if (user_id, device_id) not in _devices:
        raise HTTPException(status_code=404, detail="Device not found")
    del _devices[(user_id, device_id)]
