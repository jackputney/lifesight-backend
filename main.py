"""LifeSight backend v2 — mode router + auth + Confirm Gate + domain APIs.

POST /chat routes {transcript, mode, conversation_id} to MODE_REGISTRY
(fitness / diet / author; jarvis kept inert). Identity from
Depends(get_current_user_id). Self-hosted username/password auth via
/auth/* (AUTH_MODE=self); AUTH_MODE=dev is a local-only bypass.

Confirm Gate guards irreversible/destructive actions only (e.g. save_food_entry,
delete_scene) — not ordinary set logs or draft scene edits.

Domain APIs live in routers/v2.py (workouts, food, manuscripts, wearables),
routers/author_persistence.py (projects / documents / versions), and
routers/artifacts.py (shared generic artifacts / versions).
Google Docs Author path is abandoned on this branch (history preserved on main).

CORS is wide open for local dev — tighten before any public deploy.
"""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from modes.author.prompt import SYSTEM_PROMPT as AUTHOR_PROMPT
from modes.author.prompt import TOOLS as AUTHOR_TOOLS
from modes.brainstorm.prompt import SYSTEM_PROMPT as BRAINSTORM_PROMPT
from modes.brainstorm.prompt import TOOLS as BRAINSTORM_TOOLS
from modes.diet.prompt import SYSTEM_PROMPT as DIET_PROMPT
from modes.diet.prompt import TOOLS as DIET_TOOLS
from modes.fitness.prompt import SYSTEM_PROMPT as FITNESS_PROMPT
from modes.fitness.prompt import TOOLS as FITNESS_TOOLS
from modes.jarvis.prompt import SYSTEM_PROMPT as JARVIS_PROMPT
from modes.mail_calendar.prompt import SYSTEM_PROMPT as MAIL_CALENDAR_PROMPT
from modes.mail_calendar.prompt import TOOLS as MAIL_CALENDAR_TOOLS
from routers.artifacts import router as artifacts_router
from routers.auth import router as auth_router
from routers.author_persistence import router as author_persistence_router
from routers.v2 import router as v2_router
from shared import db
from shared.auth import assert_auth_mode_allowed, get_current_user_id
from shared.research import (
    ResearchFactCheck,
    ResearchResult,
    ResearchSource,
    clamp_research_query,
    extract_claim,
    get_research_provider,
    sanitize_research,
    unavailable_turn,
    wants_research,
)

load_dotenv()


@asynccontextmanager
async def lifespan(_: FastAPI):
    assert_auth_mode_allowed()
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


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    """Propagate/generate X-Request-ID for DB failure diagnostics."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    token = db.request_id_var.set(request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        db.request_id_var.reset(token)


@app.exception_handler(db.DatabaseUnavailableError)
async def database_unavailable_handler(request: Request, exc: db.DatabaseUnavailableError):
    return JSONResponse(
        status_code=503,
        content={"detail": "Database temporarily unavailable"},
        headers={"X-Request-ID": db.request_id_var.get()},
    )


app.include_router(auth_router)
app.include_router(v2_router)
app.include_router(author_persistence_router)
app.include_router(artifacts_router)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# v2 chat modes. health is retired (superseded by fitness + diet).
# jarvis source stays in MODE_REGISTRY / modes/jarvis/ so code is not deleted,
# but it is hidden from the public /modes list (see PUBLIC_MODE_IDS) and must
# not be reused for mail_calendar.
# settings is an iOS screen, not a chat mode.
MODE_REGISTRY = {
    "fitness": FITNESS_PROMPT,
    "diet": DIET_PROMPT,
    "author": AUTHOR_PROMPT,
    "brainstorm": BRAINSTORM_PROMPT,
    "mail_calendar": MAIL_CALENDAR_PROMPT,
    "jarvis": JARVIS_PROMPT,
}

# Authoritative public v2 mode list — exact Home/Sidebar order. Do not sort.
# Excludes jarvis (legacy, hidden) and health (retired).
PUBLIC_MODE_IDS: tuple[str, ...] = (
    "fitness",
    "diet",
    "author",
    "brainstorm",
    "mail_calendar",
)

# Per-mode Anthropic tool schemas. Modes with no tools simply aren't a key
# here — _run_model_turn treats a missing/empty list as "no tools offered".
# brainstorm / mail_calendar register empty tool lists until later slices.
MODE_TOOLS: dict[str, list[dict]] = {
    "author": AUTHOR_TOOLS,
    "fitness": FITNESS_TOOLS,
    "diet": DIET_TOOLS,
    "brainstorm": BRAINSTORM_TOOLS,
    "mail_calendar": MAIL_CALENDAR_TOOLS,
}

# A voice confirm that never arrives shouldn't stay "pending" forever. Passed
# to db.create_pending_action as expires_at; the pending_actions row itself
# is the only state — nothing is cached in this process.
PENDING_ACTION_TTL = timedelta(minutes=10)


class ChatRequest(BaseModel):
    transcript: str = Field(..., min_length=1)
    mode: str = "fitness"
    conversation_id: str | None = None
    # NOTE: user_id is NOT a request field — identity comes from the auth
    # token via Depends(get_current_user_id), so a client can never claim to
    # be another user.


class PendingAction(BaseModel):
    action_id: str
    description: str


class VisualPanel(BaseModel):
    """Optional inline visual for the chat-style UI (quarter-screen panels).
    Additive — absent/null must not break older clients."""
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    reply: str
    mode: str
    conversation_id: str
    pending_action: PendingAction | None = None
    visual_panel: VisualPanel | None = None
    # Additive Brainstorm field — null for ordinary turns and all other modes.
    research: ResearchResult | None = None


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


async def _call_model(create_kwargs: dict):
    """The Anthropic SDK call is blocking; run it off the event loop so one
    long generation doesn't stall every other request."""
    return await asyncio.to_thread(client.messages.create, **create_kwargs)


async def _run_tool(
    name: str, tool_input: dict, *, user_id: str, conversation_id: str, mode: str
) -> tuple[str, PendingAction | None]:
    """Execute one Claude tool call. Returns (tool_result text for Claude,
    pending_action to surface to the client, or None if this tool didn't
    create one). Writes go to Postgres via shared/db.py so /confirm always
    sees them — no in-memory pending state."""
    if name == "create_pending_action":
        # Confirm Gate for destructive / irreversible actions only
        # (delete_scene, overwrite_chapter, generic). Ordinary scene edits
        # and set logs do NOT use this path.
        description = str(tool_input.get("description", "")).strip()
        if not description:
            return "Error: description must be a non-empty sentence.", None
        action_type = str(tool_input.get("action_type") or "generic").strip() or "generic"
        payload = tool_input.get("payload")
        if not isinstance(payload, dict):
            payload = {k: v for k, v in tool_input.items() if k not in ("description", "action_type")}
        action_id = await db.create_pending_action(
            user_id=user_id,
            conversation_id=conversation_id,
            source_mode=mode,
            action_type=action_type,
            payload=payload or {"description": description},
            description=description,
            expires_at=datetime.now(timezone.utc) + PENDING_ACTION_TTL,
        )
        return (
            "Pending action created and shown to the user for confirmation. "
            "Do not say the action is done yet.",
            PendingAction(action_id=action_id, description=description),
        )

    return f"Error: unknown tool '{name}'.", None


async def _run_model_turn(
    *, mode: str, conversation_id: str, history: list[dict], user_id: str
) -> tuple[str, PendingAction | None]:
    """Call Claude, executing any tool calls it makes and persisting every
    turn to Postgres, until it produces a final text reply. Returns
    (reply_text, pending_action or None)."""
    create_kwargs: dict = dict(
        model=MODEL,
        max_tokens=1024,
        system=_build_system_prompt(mode),
        messages=history,
    )
    tools = MODE_TOOLS.get(mode)
    if tools:
        create_kwargs["tools"] = tools

    message = await _call_model(create_kwargs)

    pending_action: PendingAction | None = None
    # Defensive loop, not just an if: today create_pending_action never needs
    # a second round trip, but a future multi-tool mode (or Claude calling
    # more than one tool before it's done talking) needs the same shape.
    while message.stop_reason == "tool_use":
        # message.content is a list of Anthropic SDK block objects — dump to
        # plain dicts so both the in-memory history and the DB's jsonb column
        # get the same JSON-serializable shape.
        assistant_blocks = [block.model_dump() for block in message.content]
        history.append({"role": "assistant", "content": assistant_blocks})
        await db.append_message(conversation_id, "assistant", assistant_blocks)

        tool_results = []
        for block in message.content:
            if block.type != "tool_use":
                continue
            result_text, created = await _run_tool(
                block.name, block.input, user_id=user_id, conversation_id=conversation_id, mode=mode
            )
            if created is not None:
                pending_action = created
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})

        history.append({"role": "user", "content": tool_results})
        await db.append_message(conversation_id, "user", tool_results)

        message = await _call_model(create_kwargs)

    text_blocks = [block.text for block in message.content if block.type == "text"]
    if not text_blocks:
        raise HTTPException(status_code=502, detail="Model returned no text")

    reply = text_blocks[0].strip()
    await db.append_message(conversation_id, "assistant", reply)
    return reply, pending_action


# ---------------------------------------------------------------------------
# Health / identity
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/db")
async def health_db():
    """Liveness of the global asyncpg pool (SELECT 1)."""
    try:
        result = await db.check_db()
    except db.DatabaseUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable",
        ) from exc
    return {
        "status": result["status"],
        "pool_size": result["pool_size"],
        "idle_size": result["idle_size"],
    }


@app.get("/modes")
def modes():
    """Public mode catalog for clients.

    Order is product-significant (Home card order). Do not alphabetically
    sort. jarvis remains in MODE_REGISTRY for legacy/debug but is not
    advertised here.
    """
    return {"modes": list(PUBLIC_MODE_IDS)}


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
            detail=f"Unsupported mode '{mode}'. Valid modes: {sorted(PUBLIC_MODE_IDS)}",
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

    research: ResearchResult | None = None
    pending_action: PendingAction | None = None

    # Brainstorm research is opt-in via explicit verify/research/check language.
    # No Confirm Gate — read-only. Other modes never attach a research object.
    if mode == "brainstorm" and wants_research(req.transcript):
        provider = get_research_provider()
        if provider is None:
            turn = unavailable_turn(
                req.transcript,
                reason=(
                    "I can't look that up right now — web research isn't available. "
                    "We can keep brainstorming without a live search."
                ),
            )
        else:
            turn = await provider.research(
                clamp_research_query(req.transcript),
                claim=extract_claim(req.transcript),
            )
        reply = turn.reply
        # Re-sanitize every provider payload so unsupported statuses / sneaky
        # fact_check objects cannot reach the client. Treat unavailable as
        # "no real web-search call"; completed/failed as a real attempt.
        provider_called = turn.research.status != "unavailable"
        research = sanitize_research(turn.research, provider_called=provider_called)
        await db.append_message(conversation_id, "assistant", reply)
    else:
        reply, pending_action = await _run_model_turn(
            mode=mode, conversation_id=conversation_id, history=history, user_id=user_id
        )

    return ChatResponse(
        reply=reply,
        mode=mode,
        conversation_id=conversation_id,
        pending_action=pending_action,
        visual_panel=None,
        research=research,
    )


async def _execute_save_food_entry(action: dict) -> None:
    payload = action["payload"] or {}
    await db.insert_food_entry(
        str(action["user_id"]),
        method=str(payload.get("method") or "manual"),
        matched_food_name=payload.get("matched_food_name"),
        calories=payload.get("calories"),
        protein_g=payload.get("protein_g"),
        carbs_g=payload.get("carbs_g"),
        fat_g=payload.get("fat_g"),
        confidence=payload.get("confidence"),
        raw_input_ref=payload.get("raw_input_ref"),
    )


async def _execute_delete_scene(action: dict) -> str:
    payload = action["payload"] or {}
    scene_id = payload.get("scene_id")
    if not scene_id:
        return "No scene was specified to delete."
    scene = await db.get_scene(str(scene_id))
    if scene is None:
        return "That scene was already gone."
    # Ownership: scene → chapter → manuscript.user_id
    ms = await db.get_manuscript(str(scene["manuscript_id"]), str(action["user_id"]))
    if ms is None:
        raise HTTPException(status_code=403, detail="Scene does not belong to this user")
    await db.delete_scene(str(scene_id))
    return f"Deleted the scene from {scene.get('chapter_title') or 'the manuscript'}."


@app.post("/confirm", response_model=ConfirmResponse)
async def confirm(req: ConfirmRequest, user_id: str = Depends(get_current_user_id)):
    action = await db.get_pending_action(req.action_id)

    # Ownership is a hard security boundary (cross-user access), not a normal
    # Confirm Gate lifecycle state, so it stays a real error unlike the cases
    # below.
    if action is not None and str(action["user_id"]) != user_id:
        raise HTTPException(status_code=403, detail="Pending action does not belong to this user")

    if action is not None and action["status"] == "pending" and datetime.now(timezone.utc) > action["expires_at"]:
        await db.resolve_pending_action(req.action_id, "expired")
        action["status"] = "expired"

    if action is None or action["status"] != "pending":
        # Unknown, already resolved, or just-expired — all read the same to a
        # voice client: there's nothing left to confirm. One friendly spoken
        # line beats a raw 404/409 the app would have to translate.
        return ConfirmResponse(result="That action is no longer pending.")

    await db.resolve_pending_action(
        req.action_id,
        "confirmed" if req.approved else "rejected",
        confirmed_via="click",
    )

    if not req.approved:
        return ConfirmResponse(result=f"Cancelled: {action['description']}")

    if action["action_type"] == "save_food_entry":
        await _execute_save_food_entry(action)
        return ConfirmResponse(result=f"Saved: {action['description']}")

    if action["action_type"] == "delete_scene":
        result = await _execute_delete_scene(action)
        return ConfirmResponse(result=result)

    # generic / unknown action_types resolve the pending state only.
    return ConfirmResponse(result=f"Confirmed: {action['description']}")


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


# ---------------------------------------------------------------------------
# Google Docs OAuth — abandoned on v2-rebuild (Postgres-native author).
# History + working implementation remain on main @ f3d97158.
# ---------------------------------------------------------------------------

@app.get("/oauth/google/start")
async def google_oauth_start_gone():
    raise HTTPException(
        status_code=410,
        detail="Google Docs Author integration was removed in v2. Writing is Postgres-native.",
    )


@app.get("/oauth/google/callback")
async def google_oauth_callback_gone():
    raise HTTPException(
        status_code=410,
        detail="Google Docs Author integration was removed in v2. Writing is Postgres-native.",
    )
