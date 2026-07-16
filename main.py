"""Voice Companion backend — Step 3: conversation memory + Confirm Gate scaffolding.

POST /chat routes by mode (see MODE_REGISTRY), keeps multi-turn history per
conversation_id, and returns a pending_action slot for the Confirm Gate.
POST /confirm resolves a pending_action by id. Storage is in-memory and does
not survive a restart — see CONVERSATIONS and PENDING_ACTIONS below.

CORS is wide open (allow_origins=["*"]) for local dev so the iOS Simulator
can reach localhost:8000 — tighten this before deploying anywhere public.
"""
import os
import uuid
from datetime import date, datetime

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from modes.author.prompt import SYSTEM_PROMPT as AUTHOR_PROMPT
from modes.health.prompt import SYSTEM_PROMPT as HEALTH_PROMPT
from modes.jarvis.prompt import SYSTEM_PROMPT as JARVIS_PROMPT

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

# In-memory only — resets on every restart. Fine for local dev; a durable store
# (Postgres, per the target architecture) is a separate, flagged change.
CONVERSATIONS: dict[str, list[dict]] = {}
PENDING_ACTIONS: dict[str, dict] = {}


class ChatRequest(BaseModel):
    transcript: str = Field(..., min_length=1)
    mode: str = "author"
    conversation_id: str | None = None


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


def _build_system_prompt(mode: str) -> str:
    today = date.today().isoformat()
    now_local = datetime.now().strftime("%A %B %d, %Y at %I:%M %p").replace(" 0", " ")
    return (
        f"{MODE_REGISTRY[mode]}\n\n"
        f"Today's date is {today}. Current local time: {now_local}."
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/modes")
def modes():
    return {"modes": sorted(MODE_REGISTRY)}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    mode = req.mode.lower().strip()
    if mode not in MODE_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported mode '{mode}'. Valid modes: {sorted(MODE_REGISTRY)}",
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    conversation_id = req.conversation_id or str(uuid.uuid4())
    history = CONVERSATIONS.setdefault(conversation_id, [])
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
def confirm(req: ConfirmRequest):
    action = PENDING_ACTIONS.pop(req.action_id, None)
    if action is None:
        raise HTTPException(
            status_code=404,
            detail=f"No pending action with id '{req.action_id}'",
        )

    if not req.approved:
        return ConfirmResponse(result="Cancelled. Nothing was sent or created.")

    # No tool executor is wired up yet — there is currently nothing in
    # PENDING_ACTIONS to confirm in practice. This path is real and will execute
    # the actual action once a mode starts populating pending_action in /chat.
    return ConfirmResponse(
        result="Confirmed, but no tool executor is wired up yet — nothing was actually sent."
    )
