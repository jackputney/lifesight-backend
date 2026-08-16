"""LifeSight backend v2 — mode router + auth + Confirm Gate + domain APIs.

POST /chat routes {transcript, mode, conversation_id} to MODE_REGISTRY
(fitness / diet / author; jarvis kept inert). Identity from
Depends(get_current_user_id). Self-hosted username/password auth via
/auth/* (AUTH_MODE=self); AUTH_MODE=dev is a local-only bypass.

Confirm Gate guards irreversible/destructive actions only (e.g. save_food_entry,
delete_scene) — not ordinary set logs or draft scene edits.

Domain APIs live in routers/v2.py (workouts, food, manuscripts, wearables)
and routers/author_persistence.py (projects / documents / versions).
Google Docs Author path is abandoned on this branch (history preserved on main).

CORS origins come from CORS_ALLOW_ORIGINS (local default *).
Staging/production require AUTH_MODE=self + AUTH_JWT_SECRET + explicit CORS.
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
from modes.checkin.prompt import SYSTEM_PROMPT as CHECKIN_PROMPT
from modes.checkin.prompt import TOOLS as CHECKIN_TOOLS
from modes.diet.prompt import SYSTEM_PROMPT as DIET_PROMPT
from modes.diet.prompt import TOOLS as DIET_TOOLS
from modes.fitness.prompt import SYSTEM_PROMPT as FITNESS_PROMPT
from modes.fitness.prompt import TOOLS as FITNESS_TOOLS
from modes.jarvis.prompt import SYSTEM_PROMPT as JARVIS_PROMPT
from modes.mail_calendar.prompt import SYSTEM_PROMPT as MAIL_CALENDAR_PROMPT
from modes.mail_calendar.prompt import TOOLS as MAIL_CALENDAR_TOOLS
from routers.auth import router as auth_router
from routers.author_persistence import router as author_persistence_router
from routers.author_pipeline import router as author_pipeline_router
from routers.chat_stream import router as chat_stream_router
from routers.conversations import router as conversations_router
from routers.daily_checkin import router as daily_checkin_router
from routers.healthkit import router as healthkit_router
from routers.integrations_google import router as integrations_google_router
from routers.profile import router as profile_router
from routers.v2 import router as v2_router
from routers.voice import router as voice_router
from shared.mail_calendar.tools import (
    execute_mail_calendar_action,
    run_list_calendar_events,
)
from shared import db
from shared.auth import assert_auth_mode_allowed, cors_allow_origins, get_current_user_id
from shared.client_actions import (
    ClientAction,
    blocked_navigate_reply,
    empty_client_actions,
    navigate_acknowledgement,
    navigate_action,
    open_conversation_action,
    parse_navigate_command,
    refresh_profile_action,
)
from shared.context_budget import build_model_messages
from shared.conversation_summary import maybe_roll_summary
from shared.conversation_titles import title_from_user_text
from shared.daily_checkin import (
    apply_checkin_tool_update,
    compact_checkin_for_context,
    get_today_checkin,
)
from shared.open_conversation import (
    day_bounds_utc,
    open_conversation_acknowledgement,
    parse_open_conversation_command,
    resolve_open_conversation,
)
from shared.personal_context import (
    PERSONAL_CONTEXT_ENRICHMENT_POLICY,
    UPDATE_PERSONAL_CONTEXT_TOOL,
    apply_personal_context_update,
)
from shared.health.tools import run_get_recent_health_data
from shared.profile_schema import compact_profile_for_context
from shared.profile_service import get_profile
from shared.prompt_overrides import load_active_customization_block
from shared.visual_panels import (
    VisualPanel,
    exercise_visual_panel,
    parse_exercise_panel_tool_input,
)
from shared.research import (
    ResearchResult,
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

# Origins from CORS_ALLOW_ORIGINS (comma-separated). Local unset → ["*"].
# Staging/production refuse wildcard / missing allowlist at startup.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins(),
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
app.include_router(profile_router)
app.include_router(daily_checkin_router)
app.include_router(conversations_router)
app.include_router(v2_router)
app.include_router(author_persistence_router)
app.include_router(author_pipeline_router)
app.include_router(healthkit_router)
app.include_router(voice_router)
app.include_router(chat_stream_router)
app.include_router(integrations_google_router)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# v2 chat modes. health is retired (superseded by fitness + diet).
# jarvis source stays in MODE_REGISTRY / modes/jarvis/ so code is not deleted,
# but it is hidden from the public /modes list (see PUBLIC_MODE_IDS) and must
# not be reused for mail_calendar.
# checkin is a dedicated daily-check-in workflow — chat-acceptable, hidden
# from GET /modes (iOS opens it via /daily-checkin/start).
# settings is an iOS screen, not a chat mode.
MODE_REGISTRY = {
    "fitness": FITNESS_PROMPT,
    "diet": DIET_PROMPT,
    "author": AUTHOR_PROMPT,
    "brainstorm": BRAINSTORM_PROMPT,
    "mail_calendar": MAIL_CALENDAR_PROMPT,
    "jarvis": JARVIS_PROMPT,
    "checkin": CHECKIN_PROMPT,
}

# Authoritative public v2 mode list — exact Home/Sidebar order. Do not sort.
# Excludes jarvis (legacy, hidden), checkin (Home-card workflow), health (retired).
PUBLIC_MODE_IDS: tuple[str, ...] = (
    "fitness",
    "diet",
    "author",
    "brainstorm",
    "mail_calendar",
)

# Per-mode Anthropic tool schemas. Modes with no tools simply aren't a key
# here — _run_model_turn treats a missing/empty list as "no tools offered".
# Personal-context enrichment is offered on public chat modes (not jarvis).
# checkin uses only the daily-check-in tool.
_PERSONAL_CONTEXT_TOOLS = [UPDATE_PERSONAL_CONTEXT_TOOL]
MODE_TOOLS: dict[str, list[dict]] = {
    "author": list(AUTHOR_TOOLS) + _PERSONAL_CONTEXT_TOOLS,
    "fitness": list(FITNESS_TOOLS) + _PERSONAL_CONTEXT_TOOLS,
    "diet": list(DIET_TOOLS) + _PERSONAL_CONTEXT_TOOLS,
    "brainstorm": list(BRAINSTORM_TOOLS) + _PERSONAL_CONTEXT_TOOLS,
    "mail_calendar": list(MAIL_CALENDAR_TOOLS) + _PERSONAL_CONTEXT_TOOLS,
    "checkin": list(CHECKIN_TOOLS),
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


class ChatResponse(BaseModel):
    reply: str
    mode: str
    conversation_id: str
    pending_action: PendingAction | None = None
    visual_panel: VisualPanel | None = None
    # Additive Brainstorm field — null for ordinary turns and all other modes.
    research: ResearchResult | None = None
    # Always an array (never null). navigate | open_conversation | refresh_profile.
    # Ordinary turns → []. Not Confirm Gate — client-local UI actions only.
    client_actions: list[ClientAction] = Field(default_factory=list)


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


def _build_system_prompt(
    mode: str,
    *,
    profile_block: str = "",
    checkin_block: str = "",
    user_customization_block: str = "",
) -> str:
    """Assemble runtime chat system prompt.

    MODE_REGISTRY[mode] already embeds:
      IDENTITY → EPISTEMIC_GROUNDING → FEASIBILITY_AND_NON_SYCOPHANCY → MODE
    Then this function appends subordinate user customization, then
    date / profile / check-in / enrichment context.
    """
    today = date.today().isoformat()
    now_local = datetime.now().strftime("%A %B %d, %Y at %I:%M %p").replace(" 0", " ")
    parts = [MODE_REGISTRY[mode]]
    if user_customization_block.strip():
        parts.append(user_customization_block.strip())
    parts.append(f"Today's date is {today}. Current local time: {now_local}.")
    if profile_block.strip():
        parts.append(profile_block.strip())
    if checkin_block.strip() and mode in ("fitness", "diet"):
        parts.append(checkin_block.strip())
    if mode != "checkin" and mode != "jarvis":
        parts.append(PERSONAL_CONTEXT_ENRICHMENT_POLICY.strip())
    return "\n\n".join(parts)


async def _call_model(create_kwargs: dict):
    """The Anthropic SDK call is blocking; run it off the event loop so one
    long generation doesn't stall every other request."""
    return await asyncio.to_thread(client.messages.create, **create_kwargs)


async def _run_tool(
    name: str,
    tool_input: dict,
    *,
    user_id: str,
    conversation_id: str,
    mode: str,
) -> tuple[str, PendingAction | None, VisualPanel | None, list[ClientAction]]:
    """Execute one Claude tool call.

    Returns (tool_result text, pending_action or None, visual_panel or None,
    client_actions).
    """
    if name == "create_pending_action":
        description = str(tool_input.get("description", "")).strip()
        if not description:
            return "Error: description must be a non-empty sentence.", None, None, []
        action_type = str(tool_input.get("action_type") or "generic").strip() or "generic"
        payload = tool_input.get("payload")
        if not isinstance(payload, dict):
            payload = {
                k: v
                for k, v in tool_input.items()
                if k not in ("description", "action_type")
            }
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
            None,
            [],
        )

    if name == "present_exercise_panel":
        if mode != "fitness":
            return (
                "Error: present_exercise_panel is only available in fitness mode.",
                None,
                None,
                [],
            )
        try:
            data = parse_exercise_panel_tool_input(
                tool_input if isinstance(tool_input, dict) else {}
            )
        except Exception as exc:
            return f"Error: invalid exercise panel ({exc}).", None, None, []
        panel = exercise_visual_panel(data)
        return (
            "Exercise panel shown to the client. Continue with a short spoken reply.",
            None,
            panel,
            [],
        )

    if name == "update_personal_context":
        if mode == "checkin":
            return (
                "Error: update_personal_context is not available during daily check-in.",
                None,
                None,
                [],
            )
        result_text, changed = await apply_personal_context_update(user_id, tool_input)
        actions: list[ClientAction] = [refresh_profile_action()] if changed else []
        return result_text, None, None, actions

    if name == "update_daily_checkin":
        if mode != "checkin":
            return (
                "Error: update_daily_checkin is only available in checkin mode.",
                None,
                None,
                [],
            )
        result_text = await apply_checkin_tool_update(
            user_id, tool_input, conversation_id=conversation_id
        )
        return result_text, None, None, []

    if name == "list_calendar_events":
        if mode != "mail_calendar":
            return (
                "Error: list_calendar_events is only available in mail_calendar mode.",
                None,
                None,
                [],
            )
        try:
            result_text = await run_list_calendar_events(user_id, tool_input)
        except Exception:
            result_text = (
                "Error [provider_unavailable]: Google Calendar is temporarily "
                "unavailable. Try again shortly."
            )
        return result_text, None, None, []

    if name == "get_recent_health_data":
        if mode not in ("fitness", "diet"):
            return (
                "Error: get_recent_health_data is only available in fitness and "
                "diet modes.",
                None,
                None,
                [],
            )
        result_text = await run_get_recent_health_data(user_id, tool_input)
        return result_text, None, None, []

    return f"Error: unknown tool '{name}'.", None, None, []


async def _run_model_turn(
    *,
    mode: str,
    conversation_id: str,
    history_for_model: list[dict],
    user_id: str,
    system_prompt: str,
    context_meta: dict[str, Any],
) -> tuple[str, PendingAction | None, VisualPanel | None, list[ClientAction]]:
    """Call Claude with a bounded context until a final text reply.

    Tool side-effects (pending_action, exercise panel, client_actions) are
    persisted; the full transcript is already/also written by the caller and
    this function.
    """
    create_kwargs: dict = dict(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=history_for_model,
    )
    tools = MODE_TOOLS.get(mode)
    if tools:
        create_kwargs["tools"] = tools

    message = await _call_model(create_kwargs)
    input_tokens = getattr(getattr(message, "usage", None), "input_tokens", None)
    output_tokens = getattr(getattr(message, "usage", None), "output_tokens", None)

    pending_action: PendingAction | None = None
    visual_panel: VisualPanel | None = None
    client_actions: list[ClientAction] = []
    working = list(history_for_model)

    while message.stop_reason == "tool_use":
        assistant_blocks = [block.model_dump() for block in message.content]
        working.append({"role": "assistant", "content": assistant_blocks})
        await db.append_message(conversation_id, "assistant", assistant_blocks)

        tool_results = []
        for block in message.content:
            if block.type != "tool_use":
                continue
            result_text, created, panel, actions = await _run_tool(
                block.name,
                block.input,
                user_id=user_id,
                conversation_id=conversation_id,
                mode=mode,
            )
            if created is not None:
                pending_action = created
            if panel is not None:
                visual_panel = panel
            if actions:
                client_actions.extend(actions)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                }
            )

        working.append({"role": "user", "content": tool_results})
        await db.append_message(conversation_id, "user", tool_results)
        create_kwargs["messages"] = working
        message = await _call_model(create_kwargs)
        # Prefer final-turn usage when present.
        input_tokens = getattr(getattr(message, "usage", None), "input_tokens", input_tokens)
        out = getattr(getattr(message, "usage", None), "output_tokens", None)
        if out is not None:
            output_tokens = (output_tokens or 0) + out

    text_blocks = [block.text for block in message.content if block.type == "text"]
    if not text_blocks:
        raise HTTPException(status_code=502, detail="Model returned no text")

    reply = text_blocks[0].strip()
    await db.append_message(conversation_id, "assistant", reply)

    try:
        await db.insert_turn_metrics(
            conversation_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw_messages_included=int(context_meta.get("raw_messages_included") or 0),
            summary_used=bool(context_meta.get("summary_used")),
            summary_through_seq=context_meta.get("summary_through_seq"),
            approx_context_utilization=context_meta.get("approx_context_utilization"),
            supplemental={
                "estimated_message_tokens": context_meta.get("estimated_message_tokens"),
                "estimated_system_tokens": context_meta.get("estimated_system_tokens"),
            },
        )
    except Exception:
        # Metrics must never break chat.
        pass

    # Deduplicate refresh_profile if the tool ran more than once.
    deduped: list[ClientAction] = []
    seen_refresh = False
    for action in client_actions:
        if getattr(action, "type", None) == "refresh_profile":
            if seen_refresh:
                continue
            seen_refresh = True
        deduped.append(action)

    return reply, pending_action, visual_panel, deduped


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

async def _ensure_conversation(
    conversation_id: str | None, *, user_id: str, mode: str
) -> tuple[str, str]:
    """Resolve / create conversation_id with ownership checks.

    Returns (conversation_id, authoritative_mode). Stored mode wins when the
    conversation already exists — request mode is not silently rewritten onto it.
    """
    if conversation_id is None:
        conversation_id = str(uuid.uuid4())
        await db.create_conversation(conversation_id, user_id, mode, title=None)
        return conversation_id, mode
    try:
        uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="conversation_id must be a UUID")
    convo = await db.get_conversation(conversation_id)
    if convo is None:
        await db.create_conversation(conversation_id, user_id, mode, title=None)
        return conversation_id, mode
    if str(convo["user_id"]) != user_id:
        raise HTTPException(
            status_code=403, detail="conversation_id does not belong to this user"
        )
    return conversation_id, str(convo["mode"])


async def _maybe_set_title(conversation_id: str, *, mode: str, user_text: str) -> None:
    title = title_from_user_text(user_text, mode=mode)
    await db.set_conversation_title_if_empty(conversation_id, title)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user_id: str = Depends(get_current_user_id)):
    request_mode = req.mode.lower().strip()
    if request_mode not in MODE_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported mode '{request_mode}'. "
                f"Valid modes: {sorted(PUBLIC_MODE_IDS)}"
            ),
        )

    # History reopen (open_conversation) before screen navigate — more specific.
    open_intent = parse_open_conversation_command(req.transcript)
    if open_intent is not None:
        conversation_id, mode = await _ensure_conversation(
            req.conversation_id, user_id=user_id, mode=request_mode
        )
        await db.append_message(conversation_id, "user", req.transcript)
        await _maybe_set_title(
            conversation_id, mode=mode, user_text=req.transcript
        )
        bounds = day_bounds_utc(open_intent.day)
        started_after = bounds[0] if bounds else None
        started_before = bounds[1] if bounds else None
        candidates = await db.find_conversations_for_open(
            user_id,
            mode=open_intent.mode,
            started_after=started_after,
            started_before=started_before,
            limit=10,
        )
        # For "most recent" without day/mode, candidates are already newest-first.
        # Uniqueness: if more than one match for a scoped query, clarify.
        # For pure most-recent with no mode/day, take only the single newest if
        # we intentionally want last chat — that is unique by definition (top 1).
        if open_intent.mode is None and open_intent.day == "any" and open_intent.most_recent:
            candidates = candidates[:1]
        resolution = resolve_open_conversation(candidates, intent=open_intent)
        if resolution.conversation_id:
            reply = open_conversation_acknowledgement(resolution.mode)
            actions = [open_conversation_action(resolution.conversation_id)]
        else:
            reply = resolution.clarify_reply or "I couldn't find that conversation."
            actions = empty_client_actions()
        await db.append_message(conversation_id, "assistant", reply)
        return ChatResponse(
            reply=reply,
            mode=mode,
            conversation_id=conversation_id,
            pending_action=None,
            visual_panel=None,
            research=None,
            client_actions=actions,
        )

    # Global app commands (navigate) — before mode Claude / API key.
    navigate = parse_navigate_command(req.transcript)
    if navigate is not None:
        conversation_id, mode = await _ensure_conversation(
            req.conversation_id, user_id=user_id, mode=request_mode
        )
        await db.append_message(conversation_id, "user", req.transcript)
        await _maybe_set_title(
            conversation_id, mode=mode, user_text=req.transcript
        )
        if navigate.target is not None:
            reply = navigate_acknowledgement(navigate.target)
            actions = [navigate_action(navigate.target)]
        else:
            reply = blocked_navigate_reply(navigate.blocked_alias or "")
            actions = empty_client_actions()
        await db.append_message(conversation_id, "assistant", reply)
        return ChatResponse(
            reply=reply,
            mode=mode,
            conversation_id=conversation_id,
            pending_action=None,
            visual_panel=None,
            research=None,
            client_actions=actions,
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    conversation_id, mode = await _ensure_conversation(
        req.conversation_id, user_id=user_id, mode=request_mode
    )

    convo = await db.get_conversation(conversation_id)
    summary_text = (convo or {}).get("summary_text")
    summary_through_seq = (convo or {}).get("summary_through_seq")

    messages_with_seq = await db.load_messages_with_seq(conversation_id)

    async def _persist_summary(text: str, through: int) -> None:
        await db.update_conversation_summary(
            conversation_id, summary_text=text, summary_through_seq=through
        )

    summary_text, summary_through_seq, _ = await maybe_roll_summary(
        conversation_id=conversation_id,
        messages_with_seq=messages_with_seq,
        summary_text=summary_text,
        summary_through_seq=summary_through_seq,
        persist=_persist_summary,
    )

    await db.append_message(conversation_id, "user", req.transcript)
    await _maybe_set_title(conversation_id, mode=mode, user_text=req.transcript)

    # Recent raw window: messages after summary_through_seq (excluding current,
    # which we pass separately). Reload after append for complete seq list?
    # Current turn is not yet in messages_with_seq — good.
    floor = -1 if summary_through_seq is None else int(summary_through_seq)
    prior = [
        {"role": m["role"], "content": m["content"]}
        for m in messages_with_seq
        if int(m["seq"]) > floor
    ]
    current_user_message = {"role": "user", "content": req.transcript}

    profile = await get_profile(user_id)
    profile_block = compact_profile_for_context(profile)
    checkin_block = ""
    if mode in ("fitness", "diet"):
        try:
            today_checkin = await get_today_checkin(user_id)
            checkin_block = compact_checkin_for_context(today_checkin)
        except Exception:
            checkin_block = ""
    try:
        user_customization_block = await load_active_customization_block(
            user_id, mode
        )
    except Exception:
        # Prompt overrides must never break chat.
        user_customization_block = ""
    system_prompt = _build_system_prompt(
        mode,
        profile_block=profile_block,
        checkin_block=checkin_block,
        user_customization_block=user_customization_block,
    )

    built = build_model_messages(
        system_prompt=system_prompt,
        profile_block=profile_block,
        summary_text=summary_text,
        summary_through_seq=summary_through_seq,
        recent_messages=prior,
        current_user_message=current_user_message,
    )
    context_meta = {
        "raw_messages_included": built.raw_messages_included,
        "summary_used": built.summary_used,
        "summary_through_seq": built.summary_through_seq,
        "approx_context_utilization": built.approx_context_utilization,
        "estimated_message_tokens": built.estimated_message_tokens,
        "estimated_system_tokens": built.estimated_system_tokens,
    }

    research: ResearchResult | None = None
    pending_action: PendingAction | None = None
    visual_panel: VisualPanel | None = None
    client_actions = empty_client_actions()

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
        provider_called = turn.research.status != "unavailable"
        research = sanitize_research(turn.research, provider_called=provider_called)
        await db.append_message(conversation_id, "assistant", reply)
    else:
        reply, pending_action, visual_panel, client_actions = await _run_model_turn(
            mode=mode,
            conversation_id=conversation_id,
            history_for_model=built.messages,
            user_id=user_id,
            system_prompt=system_prompt,
            context_meta=context_meta,
        )

    return ChatResponse(
        reply=reply,
        mode=mode,
        conversation_id=conversation_id,
        pending_action=pending_action,
        visual_panel=visual_panel,
        research=research,
        client_actions=client_actions,
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

    if action["action_type"] in (
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
        "send_email",
    ):
        result = await execute_mail_calendar_action(
            action["action_type"],
            str(action["user_id"]),
            action.get("payload") or {},
        )
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
