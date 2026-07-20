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

Storage is Postgres via shared/db.py (asyncpg), backed by migrations
001_users_devices.sql and 002_core_schema.sql run against a Supabase
project. DATABASE_URL must be set; startup fails fast with a readable
error otherwise. Routes never touch SQL — they call shared.db functions,
same pattern shared/auth.py uses for identity.

CORS is wide open (allow_origins=["*"]) for local dev so the iOS Simulator
can reach localhost:8000 — tighten this before deploying anywhere public.
"""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager
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
from shared import db
from shared.auth import get_current_user_id

load_dotenv()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_pool()
    yield
    await db.close_pool()


app = FastAPI(title="Lifesight Backend", lifespan=lifespan)

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
async def chat(req: ChatRequest, user_id: str = Depends(get_current_user_id)):
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
        await db.create_conversation(conversation_id, user_id, mode)
    else:
        try:
            uuid.UUID(conversation_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="conversation_id must be a UUID")
        convo = await db.get_conversation(conversation_id)
        if convo is None:
            # Client sent an id we've never seen (e.g. minted before the DB
            # existed) — start fresh under that id rather than erroring out.
            await db.create_conversation(conversation_id, user_id, mode)
        elif str(convo["user_id"]) != user_id:
            raise HTTPException(status_code=403, detail="conversation_id does not belong to this user")

    history = await db.load_messages(conversation_id)
    history.append({"role": "user", "content": req.transcript})
    await db.append_message(conversation_id, "user", req.transcript)

    # The Anthropic SDK call is blocking; run it off the event loop so one
    # long generation doesn't stall every other request.
    message = await asyncio.to_thread(
        client.messages.create,
        model=MODEL,
        max_tokens=1024,
        system=_build_system_prompt(mode),
        messages=history,
    )

    text_blocks = [block.text for block in message.content if block.type == "text"]
    if not text_blocks:
        raise HTTPException(status_code=502, detail="Model returned no text")

    reply = text_blocks[0].strip()
    await db.append_message(conversation_id, "assistant", reply)

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
async def confirm(req: ConfirmRequest, user_id: str = Depends(get_current_user_id)):
    action = await db.get_pending_action(req.action_id)
    if action is None:
        raise HTTPException(
            status_code=404,
            detail=f"No pending action with id '{req.action_id}'",
        )
    if str(action["user_id"]) != user_id:
        raise HTTPException(status_code=403, detail="Pending action does not belong to this user")
    if action["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Pending action already {action['status']}")
    if datetime.now(timezone.utc) > action["expires_at"]:
        await db.resolve_pending_action(req.action_id, "expired")
        raise HTTPException(status_code=410, detail="Pending action expired before it was confirmed")

    await db.resolve_pending_action(
        req.action_id,
        "confirmed" if req.approved else "rejected",
        confirmed_via="click",
    )

    if not req.approved:
        return ConfirmResponse(result="Cancelled. Nothing was sent or created.")

    # No tool executor is wired up yet — there is currently no code path that
    # creates pending_actions rows, so this branch has nothing real to confirm
    # in practice. It will execute the actual action once a mode's tool-calling
    # starts creating rows here.
    return ConfirmResponse(
        result="Confirmed, but no tool executor is wired up yet — nothing was actually sent."
    )


# ---------------------------------------------------------------------------
# Devices (push-notification targets per user)
# ---------------------------------------------------------------------------
# Backed by the devices table (migrations/001_users_devices.sql).

@app.post("/devices", response_model=DeviceOut)
async def register_device(
    body: DeviceRegister,
    user_id: str = Depends(get_current_user_id),
):
    """Upsert a device for the current user. Called by the mobile app on launch
    and whenever the push token rotates."""
    return await db.upsert_device(user_id, body.device_id, body.push_token, body.platform)


@app.get("/devices", response_model=list[DeviceOut])
async def list_devices(user_id: str = Depends(get_current_user_id)):
    return await db.list_devices(user_id)


@app.delete("/devices/{device_id}", status_code=204)
async def remove_device(
    device_id: str,
    user_id: str = Depends(get_current_user_id),
):
    if not await db.delete_device(user_id, device_id):
        raise HTTPException(status_code=404, detail="Device not found")
