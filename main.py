"""Voice Companion backend — mode router + identity/devices + Confirm Gate.

POST /chat routes {transcript, mode, conversation_id} to the matching mode
system prompt via MODE_REGISTRY and calls Claude, keeping multi-turn history
per conversation_id. Identity comes from the auth layer
(Depends(get_current_user_id)) — the client never asserts its own user_id.
Auth is stubbed in shared/auth.py (AUTH_MODE=dev by default); swapping to
real Supabase JWT verification touches only that file.

/me and /devices provide identity plus push-target registration.

POST /confirm resolves a pending action by id (the Confirm Gate's second
half). Author Mode is wired with three real tools: create_pending_action
(generic fallback), read_doc (read-only, no confirm needed), and write_doc
(the real manuscript-write path — creates a pending action carrying the
actual text, executed for real by /confirm on approval). health/jarvis have
no tools yet and always return pending_action=null.

Storage is Postgres via shared/db.py (asyncpg), backed by migrations
001_users_devices.sql and 002_core_schema.sql run against a Supabase
project. DATABASE_URL must be set; startup fails fast with a readable
error otherwise. Routes never touch SQL — they call shared.db functions,
same pattern shared/auth.py uses for identity. Every tool handler follows
the same rule: it calls shared.db functions rather than keeping its own
state, so /chat (writer) and /confirm (reader) always agree on what's
pending.

GET /oauth/google/start + /oauth/google/callback run the one-time Google
OAuth handshake so Author Mode can read/write a real Google Doc. Tokens are
stored encrypted at rest (shared/crypto.py) — shared/db.py and Postgres
never see plaintext. The OAuth `state` param is HMAC-signed
(OAUTH_STATE_SECRET) rather than a bare user_id, so the callback can't be
tricked into linking a Google account to the wrong LIFESIGHT user.

CORS is wide open (allow_origins=["*"]) for local dev so the iOS Simulator
can reach localhost:8000 — tighten this before deploying anywhere public.
"""
import asyncio
import hashlib
import hmac
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from modes.author.prompt import SYSTEM_PROMPT as AUTHOR_PROMPT
from modes.author.prompt import TOOLS as AUTHOR_TOOLS
from modes.health.prompt import SYSTEM_PROMPT as HEALTH_PROMPT
from modes.jarvis.prompt import SYSTEM_PROMPT as JARVIS_PROMPT
from shared import crypto, db, google_docs
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

# Per-mode Anthropic tool schemas. Modes with no tools simply aren't a key
# here — _run_model_turn treats a missing/empty list as "no tools offered".
MODE_TOOLS: dict[str, list[dict]] = {
    "author": AUTHOR_TOOLS,
}

# A voice confirm that never arrives shouldn't stay "pending" forever. Passed
# to db.create_pending_action as expires_at; the pending_actions row itself
# is the only state — nothing is cached in this process.
PENDING_ACTION_TTL = timedelta(minutes=10)

# How long a Google OAuth `state` token is valid for — the gap between
# hitting /oauth/google/start and Google redirecting back to the callback.
OAUTH_STATE_TTL_SECONDS = 600


class GoogleNotConnectedError(Exception):
    """Raised when a Google-dependent tool or /confirm execution can't get
    valid credentials — no OAuth connection yet, or an expired access token
    with no refresh token to fall back on. Callers turn this into a natural
    spoken message rather than a raw framework error, since everything in
    this app is read aloud."""


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


async def _call_model(create_kwargs: dict):
    """The Anthropic SDK call is blocking; run it off the event loop so one
    long generation doesn't stall every other request."""
    return await asyncio.to_thread(client.messages.create, **create_kwargs)


def _sign_oauth_state(user_id: str) -> str:
    """Build a `state` value for the Google OAuth redirect that carries
    user_id but can't be forged. Without this, anyone could call
    /oauth/google/callback directly with state=<victim's user_id> and link
    their own Google account to someone else's LIFESIGHT account — state is
    the only thing tying the callback back to who started the flow, since
    Google's redirect carries no Authorization header."""
    secret = os.environ["OAUTH_STATE_SECRET"].encode()
    issued_at = str(int(time.time()))
    signature = hmac.new(secret, f"{user_id}:{issued_at}".encode(), hashlib.sha256).hexdigest()
    return f"{user_id}:{issued_at}:{signature}"


def _verify_oauth_state(state: str) -> str:
    """Returns the user_id embedded in a state token, or raises ValueError if
    it's missing, malformed, forged, or too old to trust."""
    try:
        user_id, issued_at, signature = state.split(":", 2)
    except ValueError:
        raise ValueError("Malformed state") from None
    secret = os.environ["OAUTH_STATE_SECRET"].encode()
    expected = hmac.new(secret, f"{user_id}:{issued_at}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("state signature does not match — possible forgery attempt")
    if time.time() - int(issued_at) > OAUTH_STATE_TTL_SECONDS:
        raise ValueError("state expired — restart the connect flow")
    return user_id


def _to_aware_utc(dt: datetime | None) -> datetime | None:
    """google-auth returns naive UTC datetimes for token expiry; Postgres
    (via asyncpg) needs timezone-aware ones to compare/store consistently."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _get_valid_google_credentials(user_id: str) -> tuple[str, str | None, list[str]]:
    """Return (access_token, refresh_token, scopes) good to use right now,
    transparently refreshing via Google and persisting the new token first if
    the stored one has expired. Raises GoogleNotConnectedError if there's
    nothing usable — callers decide how to phrase that for the user."""
    row = await db.get_oauth_credentials(user_id, provider="google")
    if row is None:
        raise GoogleNotConnectedError("No Google account connected for this user")

    access_token = crypto.decrypt(row["access_token_enc"])
    refresh_token = crypto.decrypt(row["refresh_token_enc"]) if row["refresh_token_enc"] else None
    scopes = row["scopes"]
    expires_at = row["expires_at"]

    if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
        if not refresh_token:
            raise GoogleNotConnectedError("Google access expired and there is no refresh token")
        refreshed = await asyncio.to_thread(google_docs.refresh_access_token, refresh_token, scopes)
        access_token = refreshed["access_token"]
        refresh_token = refreshed["refresh_token"] or refresh_token
        await db.save_oauth_credentials(
            user_id=user_id,
            provider="google",
            access_token_enc=crypto.encrypt(access_token),
            refresh_token_enc=crypto.encrypt(refresh_token),
            scopes=scopes,
            expires_at=_to_aware_utc(refreshed["expiry"]),
        )

    return access_token, refresh_token, scopes


async def _run_tool(
    name: str, tool_input: dict, *, user_id: str, conversation_id: str, mode: str
) -> tuple[str, PendingAction | None]:
    """Execute one Claude tool call. Returns (tool_result text for Claude,
    pending_action to surface to the client, or None if this tool didn't
    create one). The write goes straight to Postgres via shared/db.py so
    /confirm's read (also shared/db.py) always sees it — no in-memory state."""
    if name == "create_pending_action":
        # Generic fallback for an action type that doesn't have its own tool
        # yet (e.g. a future send_email/create_event). Author Mode's real
        # manuscript writes go through write_doc below instead, which
        # carries actual text — this one only ever carries a description, so
        # /confirm has nothing real to execute for it (see the TODO there).
        description = str(tool_input.get("description", "")).strip()
        if not description:
            return "Error: description must be a non-empty sentence.", None
        action_id = await db.create_pending_action(
            user_id=user_id,
            conversation_id=conversation_id,
            source_mode=mode,
            action_type="generic",
            payload={"description": description},
            description=description,
            expires_at=datetime.now(timezone.utc) + PENDING_ACTION_TTL,
        )
        return (
            "Pending action created and shown to the user for confirmation.",
            PendingAction(action_id=action_id, description=description),
        )

    if name == "read_doc":
        doc = await db.get_writing_document(user_id, doc_type="manuscript")
        if doc is None:
            return (
                "Error: no manuscript document is connected yet. Tell the "
                "user they need to connect their Google account first.",
                None,
            )
        try:
            access_token, refresh_token, scopes = await _get_valid_google_credentials(user_id)
        except GoogleNotConnectedError:
            return (
                "Error: Google account not connected or needs reconnecting. "
                "Tell the user they need to (re)connect their Google account.",
                None,
            )
        text = await asyncio.to_thread(
            google_docs.get_document_text, access_token, refresh_token, scopes, doc["google_doc_id"]
        )
        return (text.strip() or "The manuscript is currently empty."), None

    if name == "write_doc":
        text = str(tool_input.get("text", "")).strip()
        description = str(tool_input.get("description", "")).strip()
        if not text or not description:
            return "Error: text and description must both be non-empty.", None
        doc = await db.get_writing_document(user_id, doc_type="manuscript")
        if doc is None:
            return (
                "Error: no manuscript document is connected yet. Tell the "
                "user they need to connect their Google account first.",
                None,
            )
        # The pending action carries the real text/target now, unlike
        # create_pending_action's description-only payload — /confirm reads
        # this back and performs the actual Docs write on approval.
        action_id = await db.create_pending_action(
            user_id=user_id,
            conversation_id=conversation_id,
            source_mode=mode,
            action_type="insert_manuscript",
            payload={
                "document_id": str(doc["id"]),
                "google_doc_id": doc["google_doc_id"],
                "text": text,
            },
            description=description,
            expires_at=datetime.now(timezone.utc) + PENDING_ACTION_TTL,
        )
        return (
            "Pending action created and shown to the user for confirmation.",
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

    reply, pending_action = await _run_model_turn(
        mode=mode, conversation_id=conversation_id, history=history, user_id=user_id
    )

    return ChatResponse(
        reply=reply,
        mode=mode,
        conversation_id=conversation_id,
        pending_action=pending_action,
    )


async def _execute_insert_manuscript(action: dict) -> None:
    """The real Google Docs write for an approved insert_manuscript pending
    action. Raises GoogleNotConnectedError if credentials aren't usable;
    the caller (POST /confirm) turns that into a spoken message."""
    payload = action["payload"] or {}
    google_doc_id = payload.get("google_doc_id")
    text = payload.get("text")
    document_id = payload.get("document_id")
    if not google_doc_id or text is None or not document_id:
        # A pending action created before write_doc existed (e.g. via the
        # generic create_pending_action fallback) has no real write target —
        # nothing to execute, resolve as confirmed with no side effect.
        return
    access_token, refresh_token, scopes = await _get_valid_google_credentials(str(action["user_id"]))
    revision_id = await asyncio.to_thread(
        google_docs.append_text, access_token, refresh_token, scopes, google_doc_id, text
    )
    await db.update_writing_document_revision(document_id, revision_id)


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

    if action["action_type"] == "insert_manuscript":
        try:
            await _execute_insert_manuscript(action)
        except GoogleNotConnectedError:
            return ConfirmResponse(
                result="I couldn't make that change — your Google account needs to be reconnected."
            )
    # TODO: other action_types (send_email, create_event, log_meal, ...) have
    # no real executor yet. create_pending_action's "generic" fallback still
    # only resolves the pending state without performing anything real —
    # extend this dispatch as more modes get their own real tools.

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
# Google OAuth (Author Mode's Google Docs connection)
# ---------------------------------------------------------------------------
# Not part of the frozen /chat|/confirm|/me|/devices contract — this is a
# browser-facing handshake, not something the iOS client calls as JSON. The
# app should open /oauth/google/start in a system browser/SFSafariViewController
# and let Google's redirect land on /oauth/google/callback; there is nothing
# for the app to parse out of either response today.

@app.get("/oauth/google/start")
async def google_oauth_start(user_id: str = Depends(get_current_user_id)):
    """Redirect to Google's consent screen. state is this user's id, signed
    so the callback can trust it (see _sign_oauth_state)."""
    state = _sign_oauth_state(user_id)
    auth_url = await asyncio.to_thread(google_docs.build_auth_url, state)
    return RedirectResponse(auth_url)


@app.get("/oauth/google/callback", response_class=HTMLResponse)
async def google_oauth_callback(code: str, state: str):
    """Google redirects here after consent. No Depends(get_current_user_id)
    — this request comes from Google's server via the user's browser, not
    from the iOS app, so there's no Bearer token to check. Identity comes
    entirely from the signed state param instead."""
    try:
        user_id = _verify_oauth_state(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid OAuth state: {exc}")

    tokens = await asyncio.to_thread(google_docs.exchange_code, code, state)
    await db.save_oauth_credentials(
        user_id=user_id,
        provider="google",
        access_token_enc=crypto.encrypt(tokens["access_token"]),
        refresh_token_enc=crypto.encrypt(tokens["refresh_token"]) if tokens["refresh_token"] else None,
        scopes=tokens["scopes"],
        expires_at=_to_aware_utc(tokens["expiry"]),
    )

    # "We will use a fresh doc" — create the manuscript on first connection
    # rather than asking the user to pick an existing file (this user
    # navigates by voice; a file picker isn't a usable flow). Only do this
    # once — a re-connect (expired/revoked token) shouldn't spawn a second doc.
    existing = await db.get_writing_document(user_id, doc_type="manuscript")
    if existing is None:
        google_doc_id = await asyncio.to_thread(
            google_docs.create_document,
            tokens["access_token"],
            tokens["refresh_token"],
            tokens["scopes"],
            "LIFESIGHT Manuscript",
        )
        await db.create_writing_document(user_id, google_doc_id, "LIFESIGHT Manuscript")

    return "<html><body><h1>Google account connected.</h1><p>You can close this window.</p></body></html>"
