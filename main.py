"""Voice Companion backend — mode router + identity/devices + Confirm Gate.

POST /chat routes {transcript, mode, conversation_id} to the matching mode
system prompt via MODE_REGISTRY and calls Claude, keeping multi-turn history
per conversation_id. Identity comes from the auth layer
(Depends(get_current_user_id)) — the client never asserts its own user_id.
Auth is stubbed in shared/auth.py (AUTH_MODE=dev by default); swapping to
real Supabase JWT verification touches only that file.

/me and /devices provide identity plus push-target registration.

POST /confirm resolves a pending action by id (the Confirm Gate's second
half). No mode populates pending_action yet (no tool-calling is wired up),
so it's always null in /chat responses today.

Storage (CONVERSATIONS, PENDING_ACTIONS, _devices) is in-memory and does not
survive a restart. Field shapes mirror the Postgres tables drafted in
migrations/002_core_schema.sql (conversations/messages, pending_actions) so
swapping to real DB calls later touches storage only, not route signatures —
same pattern _devices already uses for migrations/001_users_devices.sql.

CORS is wide open (allow_origins=["*"]) for local dev so the iOS Simulator
can reach localhost:8000 — tighten this before deploying anywhere public.
"""
import os
import uuid
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

import anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from modes.author.prompt import SYSTEM_PROMPT as AUTHOR_PROMPT
from modes.health.prompt import SYSTEM_PROMPT as HEALTH_PROMPT
from modes.jarvis.prompt import SYSTEM_PROMPT as JARVIS_PROMPT
from shared.auth import get_current_user_id

load_dotenv()

app = FastAPI(title="Lifesight Backend")

# Wide open for local dev only (Simulator/browser calls from any origin). Lock
# this down to the real app's origin(s) before deploying anywhere public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

MODE_REGISTRY = {
    "author": AUTHOR_PROMPT,
    "health": HEALTH_PROMPT,
    "jarvis": JARVIS_PROMPT,
}

# In-memory only — resets on every restart. A durable store (Postgres, per
# migrations/002_core_schema.sql) is a separate, flagged change.
# CONVERSATIONS mirrors conversations/messages: keyed by conversation_id,
# scoped to the user who started it.
CONVERSATIONS: dict[str, dict] = {}  # conversation_id -> {user_id, mode, messages}

# PENDING_ACTIONS mirrors the pending_actions table shape (status, expiry,
# payload) even though nothing populates it yet — see ChatResponse note below.
PENDING_ACTIONS: dict[str, dict] = {}  # action_id -> pending_actions-shaped row

_devices: dict[tuple[str, str], dict] = {}  # (user_id, device_id) -> row


class ChatRequest(BaseModel):
    transcript: str = Field(..., min_length=1)
    mode: str = "author"
    conversation_id: str | None = None
    # NOTE: user_id is NOT a request field — identity comes from the auth
    # token via Depends(get_current_user_id), so a client can never claim to
    # be another user.


class PendingAction(BaseModel):
    action_id: str
    description: str


class ChatResponse(BaseModel):
    reply: str
    mode: str
    conversation_id: str
    pending_action: PendingAction | None = None


class ConfirmRequest(BaseModel):
    action_id: str
    approved: bool


class ConfirmResponse(BaseModel):
    result: str


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
# Chat (mode router) + Confirm Gate
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

    conversation_id = req.conversation_id
    if conversation_id is None:
        conversation_id = str(uuid.uuid4())
        CONVERSATIONS[conversation_id] = {"user_id": user_id, "mode": mode, "messages": []}
    else:
        convo = CONVERSATIONS.get(conversation_id)
        if convo is None:
            # Client sent an id we've never seen (e.g. after a server restart) —
            # start fresh under that id rather than erroring the user out.
            CONVERSATIONS[conversation_id] = {"user_id": user_id, "mode": mode, "messages": []}
        elif convo["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="conversation_id does not belong to this user")

    history = CONVERSATIONS[conversation_id]["messages"]
    history.append({"role": "user", "content": req.transcript})

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_build_system_prompt(mode),
        messages=history,
    )

    text_blocks = [block.text for block in message.content if block.type == "text"]
    if not text_blocks:
        raise HTTPException(status_code=502, detail="Model returned no text")

    reply = text_blocks[0].strip()
    history.append({"role": "assistant", "content": reply})

    # No tool-calling is wired up yet (see README), so no code path constructs a
    # pending_action today. The field is real, not a placeholder — it stays null
    # until a mode actually proposes an irreversible action.
    return ChatResponse(
        reply=reply,
        mode=mode,
        conversation_id=conversation_id,
        pending_action=None,
    )


@app.post("/confirm", response_model=ConfirmResponse)
def confirm(req: ConfirmRequest, user_id: str = Depends(get_current_user_id)):
    action = PENDING_ACTIONS.get(req.action_id)
    if action is None:
        raise HTTPException(
            status_code=404,
            detail=f"No pending action with id '{req.action_id}'",
        )
    if action["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Pending action does not belong to this user")
    if action["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Pending action already {action['status']}")
    if datetime.now(timezone.utc) > action["expires_at"]:
        action["status"] = "expired"
        raise HTTPException(status_code=410, detail="Pending action expired before it was confirmed")

    action["status"] = "confirmed" if req.approved else "rejected"
    action["confirmed_via"] = "click"
    action["resolved_at"] = datetime.now(timezone.utc)

    if not req.approved:
        return ConfirmResponse(result="Cancelled. Nothing was sent or created.")

    # No tool executor is wired up yet — there is currently no code path that
    # populates PENDING_ACTIONS, so this branch has nothing real to confirm in
    # practice. It will execute the actual action once a mode's tool-calling
    # starts creating rows here.
    return ConfirmResponse(
        result="Confirmed, but no tool executor is wired up yet — nothing was actually sent."
    )


# ---------------------------------------------------------------------------
# Devices (push-notification targets per user)
# ---------------------------------------------------------------------------
# In-memory stub so the API runs today. Swap _devices for real DB calls
# (asyncpg) once the database is up — route signatures and auth wiring stay
# identical. Backed by migrations/001_users_devices.sql.

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
