"""v2 API routes: auth, workouts, food, manuscripts, wearables.

Mounted from main.py. Identity always via Depends(get_current_user_id) except
the unauthenticated Terra webhook (signature-verified when secret is set).
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import anthropic
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from shared import db, supabase_auth, terra
from shared.auth import get_current_user_id

router = APIRouter()

PENDING_ACTION_TTL_MINUTES = 10
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def _anthropic() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    return anthropic.Anthropic(api_key=key)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class SignUpRequest(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)


class SignInRequest(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)


class MagicLinkRequest(BaseModel):
    email: str = Field(..., min_length=3)


class AppleSignInRequest(BaseModel):
    id_token: str = Field(..., min_length=10)
    nonce: Optional[str] = None


class AuthSessionOut(BaseModel):
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    token_type: str = "bearer"
    user_id: Optional[str] = None
    email: Optional[str] = None


@router.post("/auth/signup", response_model=AuthSessionOut)
async def auth_signup(body: SignUpRequest):
    return AuthSessionOut(**await supabase_auth.sign_up(body.email, body.password))


@router.post("/auth/login", response_model=AuthSessionOut)
async def auth_login(body: SignInRequest):
    return AuthSessionOut(**await supabase_auth.sign_in_password(body.email, body.password))


@router.post("/auth/magic-link")
async def auth_magic_link(body: MagicLinkRequest):
    return await supabase_auth.request_magic_link(body.email)


@router.post("/auth/apple", response_model=AuthSessionOut)
async def auth_apple(body: AppleSignInRequest):
    return AuthSessionOut(**await supabase_auth.sign_in_apple(body.id_token, body.nonce))


# ---------------------------------------------------------------------------
# Fitness
# ---------------------------------------------------------------------------

class StartSessionRequest(BaseModel):
    plan_day_id: Optional[str] = None


class VoiceLogRequest(BaseModel):
    session_id: str
    transcript: str = Field(..., min_length=1)


@router.post("/workouts/session/start")
async def workouts_session_start(
    body: StartSessionRequest,
    user_id: str = Depends(get_current_user_id),
):
    session = await db.start_workout_session(user_id, body.plan_day_id)
    return {
        "session_id": str(session["id"]),
        "status": session["status"],
        "session_date": str(session["session_date"]),
        "plan_day_id": str(session["plan_day_id"]) if session["plan_day_id"] else None,
        "started_at": session["started_at"].isoformat(),
    }


@router.get("/workouts/session/{session_id}/state")
async def workouts_session_state(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    session = await db.get_workout_session(session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Workout session not found")

    exercises: list[dict] = []
    if session["plan_day_id"]:
        exercises = await db.list_planned_exercises_for_day(str(session["plan_day_id"]))

    logs = await db.list_set_logs(session_id)
    # Current exercise = first planned exercise that still has remaining sets,
    # else last logged exercise, else first planned.
    current_exercise = None
    current_set_number = 1
    rest_seconds = None
    if exercises:
        logs_by_ex: dict[str, list[dict]] = {}
        for lg in logs:
            logs_by_ex.setdefault(str(lg["exercise_id"]), []).append(lg)
        for ex in exercises:
            eid = str(ex["id"])
            done = len(logs_by_ex.get(eid, []))
            target = ex["target_sets"] or 0
            if done < target or target == 0 and done == 0:
                current_exercise = ex
                current_set_number = done + 1
                rest_seconds = ex["rest_seconds"]
                break
        if current_exercise is None:
            current_exercise = exercises[-1]
            current_set_number = len(logs_by_ex.get(str(current_exercise["id"]), [])) + 1
            rest_seconds = current_exercise["rest_seconds"]

    return {
        "session_id": str(session["id"]),
        "status": session["status"],
        "current_exercise": (
            {
                "id": str(current_exercise["id"]),
                "name": current_exercise["name"],
                "target_sets": current_exercise["target_sets"],
                "target_reps": current_exercise["target_reps"],
                "rest_seconds": current_exercise["rest_seconds"],
            }
            if current_exercise
            else None
        ),
        "current_set_number": current_set_number,
        "rest_seconds": rest_seconds,
        "sets_logged": len(logs),
    }


@router.post("/workouts/voice-log")
async def workouts_voice_log(
    body: VoiceLogRequest,
    user_id: str = Depends(get_current_user_id),
):
    session = await db.get_workout_session(body.session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Workout session not found")
    if session["status"] != "active":
        raise HTTPException(status_code=409, detail="Workout session is not active")
    if not session["plan_day_id"]:
        raise HTTPException(
            status_code=400,
            detail="Session has no plan day — cannot match exercises for voice logging",
        )

    exercises = await db.list_planned_exercises_for_day(str(session["plan_day_id"]))
    if not exercises:
        raise HTTPException(status_code=400, detail="No planned exercises for this day")

    catalog = [
        {
            "id": str(ex["id"]),
            "name": ex["name"],
            "target_sets": ex["target_sets"],
            "target_reps": ex["target_reps"],
        }
        for ex in exercises
    ]
    parsed = await _parse_voice_sets(body.transcript, catalog)
    if not parsed:
        raise HTTPException(
            status_code=422,
            detail="Could not parse any sets from that utterance. Try saying reps and weight.",
        )

    existing = await db.list_set_logs(body.session_id)
    next_set_by_ex: dict[str, int] = {}
    for lg in existing:
        eid = str(lg["exercise_id"])
        next_set_by_ex[eid] = max(next_set_by_ex.get(eid, 0), int(lg["set_number"])) + 0
    for eid in list(next_set_by_ex):
        next_set_by_ex[eid] = next_set_by_ex[eid]  # already max set number
    # Recompute properly as max+1
    counts: dict[str, int] = {}
    for lg in existing:
        eid = str(lg["exercise_id"])
        counts[eid] = max(counts.get(eid, 0), int(lg["set_number"]))

    logged: list[dict] = []
    pr_announcements: list[str] = []
    name_by_id = {str(ex["id"]): ex["name"] for ex in exercises}

    for item in parsed:
        eid = item["exercise_id"]
        if eid not in name_by_id:
            continue
        set_number = int(item.get("set_number") or (counts.get(eid, 0) + 1))
        reps = item.get("reps")
        weight = item.get("weight")
        row = await db.insert_set_log(
            body.session_id, eid, set_number, reps, weight, source="voice"
        )
        counts[eid] = max(counts.get(eid, 0), set_number)
        logged.append(
            {
                "id": str(row["id"]),
                "exercise_id": eid,
                "exercise_name": name_by_id[eid],
                "set_number": set_number,
                "reps": reps,
                "weight": weight,
            }
        )
        if reps is not None and weight is not None:
            prev = await db.get_personal_record(user_id, eid, int(reps))
            if prev is None or float(weight) > float(prev["weight"]):
                await db.upsert_personal_record(user_id, eid, int(reps), float(weight))
                pr_announcements.append(
                    f"New personal record: {name_by_id[eid]}, {reps} reps at {weight}."
                )

    visual_panel = None
    if logged:
        visual_panel = {
            "type": "workout_sets",
            "data": {"session_id": body.session_id, "sets": logged},
        }

    return {
        "sets": logged,
        "pr_announcements": pr_announcements,
        "reply": (
            (" ".join(pr_announcements) + " " if pr_announcements else "")
            + f"Logged {len(logged)} set{'s' if len(logged) != 1 else ''}."
        ).strip(),
        "visual_panel": visual_panel,
        "pending_action": None,
    }


async def _parse_voice_sets(transcript: str, catalog: list[dict]) -> list[dict]:
    """Ask Claude to parse an utterance into structured set logs."""
    client = _anthropic()
    system = (
        "Parse a gym voice log into JSON only. Return a JSON object "
        '{"sets":[{"exercise_id":"...","set_number":null,"reps":8,"weight":135,"count":1}]} '
        "where count>1 expands repeated identical sets (e.g. '5 sets of 5 at 185'). "
        "Match exercise_id from the provided catalog by name (fuzzy OK). "
        "If ambiguous, pick the best catalog match. No prose."
    )
    user = json.dumps({"transcript": transcript, "exercises": catalog})

    def _call():
        return client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

    msg = await asyncio.to_thread(_call)
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    # Tolerate fenced JSON
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    raw_sets = data.get("sets") if isinstance(data, dict) else data
    if not isinstance(raw_sets, list):
        return []

    expanded: list[dict] = []
    valid_ids = {c["id"] for c in catalog}
    for item in raw_sets:
        if not isinstance(item, dict):
            continue
        eid = item.get("exercise_id")
        if eid not in valid_ids:
            # try match by name
            name = (item.get("exercise_name") or "").lower()
            for c in catalog:
                if name and name in c["name"].lower():
                    eid = c["id"]
                    break
        if eid not in valid_ids:
            continue
        count = int(item.get("count") or 1)
        count = max(1, min(count, 20))
        for _ in range(count):
            expanded.append(
                {
                    "exercise_id": eid,
                    "set_number": item.get("set_number"),
                    "reps": item.get("reps"),
                    "weight": item.get("weight"),
                }
            )
    return expanded


# ---------------------------------------------------------------------------
# Diet
# ---------------------------------------------------------------------------

class FoodDraft(BaseModel):
    method: str
    matched_food_name: Optional[str] = None
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    confidence: Optional[float] = None
    raw_input_ref: Optional[str] = None


class FoodPhotoRequest(BaseModel):
    image_base64: str = Field(..., min_length=1)
    media_type: str = "image/jpeg"


class FoodBarcodeRequest(BaseModel):
    barcode: str = Field(..., min_length=4)


class FoodVoiceRequest(BaseModel):
    transcript: str = Field(..., min_length=1)


class FoodEntriesRequest(BaseModel):
    draft: FoodDraft


@router.post("/food/photo")
async def food_photo(
    body: FoodPhotoRequest,
    user_id: str = Depends(get_current_user_id),
):
    client = _anthropic()

    def _call():
        return client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=(
                "Estimate the meal in the image. Return JSON only: "
                '{"matched_food_name":str,"calories":number,"protein_g":number,'
                '"carbs_g":number,"fat_g":number,"confidence":0-1}'
            ),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": body.media_type,
                                "data": body.image_base64,
                            },
                        },
                        {"type": "text", "text": "Identify the food and estimate macros."},
                    ],
                }
            ],
        )

    msg = await asyncio.to_thread(_call)
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    draft = _json_object(text)
    return {
        "draft": FoodDraft(
            method="photo",
            matched_food_name=draft.get("matched_food_name"),
            calories=draft.get("calories"),
            protein_g=draft.get("protein_g"),
            carbs_g=draft.get("carbs_g"),
            fat_g=draft.get("fat_g"),
            confidence=draft.get("confidence"),
            raw_input_ref="photo",
        ).model_dump(),
        "pending_action": None,
    }


@router.post("/food/barcode")
async def food_barcode(
    body: FoodBarcodeRequest,
    user_id: str = Depends(get_current_user_id),
):
    url = f"https://world.openfoodfacts.org/api/v2/product/{body.barcode}.json"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers={"User-Agent": "LifeSight/2.0 (diet-barcode)"})
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="Open Food Facts lookup failed")
    data = resp.json()
    if data.get("status") != 1:
        raise HTTPException(status_code=404, detail="Barcode not found in Open Food Facts")
    product = data.get("product") or {}
    nutriments = product.get("nutriments") or {}
    name = product.get("product_name") or product.get("generic_name") or "Unknown product"
    return {
        "draft": FoodDraft(
            method="barcode",
            matched_food_name=name,
            calories=_num(nutriments.get("energy-kcal_serving") or nutriments.get("energy-kcal_100g")),
            protein_g=_num(nutriments.get("proteins_serving") or nutriments.get("proteins_100g")),
            carbs_g=_num(nutriments.get("carbohydrates_serving") or nutriments.get("carbohydrates_100g")),
            fat_g=_num(nutriments.get("fat_serving") or nutriments.get("fat_100g")),
            confidence=0.9,
            raw_input_ref=body.barcode,
        ).model_dump(),
        "pending_action": None,
    }


@router.post("/food/voice")
async def food_voice(
    body: FoodVoiceRequest,
    user_id: str = Depends(get_current_user_id),
):
    client = _anthropic()

    def _call():
        return client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=(
                "Parse a spoken meal description into JSON only: "
                '{"matched_food_name":str,"calories":number,"protein_g":number,'
                '"carbs_g":number,"fat_g":number,"confidence":0-1}'
            ),
            messages=[{"role": "user", "content": body.transcript}],
        )

    msg = await asyncio.to_thread(_call)
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    draft = _json_object(text)
    return {
        "draft": FoodDraft(
            method="voice",
            matched_food_name=draft.get("matched_food_name"),
            calories=draft.get("calories"),
            protein_g=draft.get("protein_g"),
            carbs_g=draft.get("carbs_g"),
            fat_g=draft.get("fat_g"),
            confidence=draft.get("confidence"),
            raw_input_ref=body.transcript[:500],
        ).model_dump(),
        "pending_action": None,
    }


@router.post("/food/entries")
async def food_entries(
    body: FoodEntriesRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Stage a food save through the Confirm Gate — does NOT write food_entries yet."""
    draft = body.draft
    if draft.method not in ("photo", "barcode", "voice", "manual"):
        raise HTTPException(status_code=400, detail="Invalid food method")
    name = draft.matched_food_name or "that food"
    description = f"Save a food log for {name}."
    if draft.calories is not None:
        description = f"Save a food log for {name}, about {int(draft.calories)} calories."

    from datetime import timedelta

    action_id = await db.create_pending_action(
        user_id=user_id,
        conversation_id=None,
        source_mode="diet",
        action_type="save_food_entry",
        payload=draft.model_dump(),
        description=description,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=PENDING_ACTION_TTL_MINUTES),
    )
    return {
        "pending_action": {"action_id": action_id, "description": description},
        "draft": draft.model_dump(),
    }


def _json_object(text: str) -> dict[str, Any]:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:].strip()
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Author (Postgres-native)
# ---------------------------------------------------------------------------

class ManuscriptCreate(BaseModel):
    title: str = Field(..., min_length=1)


class ChapterCreate(BaseModel):
    title: str = Field(..., min_length=1)
    sort_order: int = 0


class SceneCreate(BaseModel):
    content: str = ""
    sort_order: int = 0


class SceneUpdate(BaseModel):
    content: str


class BrainstormRequest(BaseModel):
    manuscript_id: str
    transcript: str = Field(..., min_length=1)
    linked_scene_id: Optional[str] = None


@router.post("/manuscripts")
async def manuscripts_create(
    body: ManuscriptCreate,
    user_id: str = Depends(get_current_user_id),
):
    row = await db.create_manuscript(user_id, body.title)
    return {"id": str(row["id"]), "title": row["title"], "created_at": row["created_at"].isoformat()}


@router.post("/manuscripts/{manuscript_id}/chapters")
async def manuscripts_create_chapter(
    manuscript_id: str,
    body: ChapterCreate,
    user_id: str = Depends(get_current_user_id),
):
    ms = await db.get_manuscript(manuscript_id, user_id)
    if ms is None:
        raise HTTPException(status_code=404, detail="Manuscript not found")
    row = await db.create_chapter(manuscript_id, body.title, body.sort_order)
    return {
        "id": str(row["id"]),
        "manuscript_id": manuscript_id,
        "title": row["title"],
        "sort_order": row["sort_order"],
    }


@router.post("/manuscripts/{manuscript_id}/chapters/{chapter_id}/scenes")
async def manuscripts_create_scene(
    manuscript_id: str,
    chapter_id: str,
    body: SceneCreate,
    user_id: str = Depends(get_current_user_id),
):
    ms = await db.get_manuscript(manuscript_id, user_id)
    if ms is None:
        raise HTTPException(status_code=404, detail="Manuscript not found")
    chapters = await db.list_chapters(manuscript_id)
    if not any(str(c["id"]) == chapter_id for c in chapters):
        raise HTTPException(status_code=404, detail="Chapter not found on this manuscript")
    row = await db.create_scene(chapter_id, body.content, body.sort_order)
    return {
        "id": str(row["id"]),
        "chapter_id": chapter_id,
        "word_count": row["word_count"],
        "sort_order": row["sort_order"],
        "updated_at": row["updated_at"].isoformat(),
    }


@router.patch("/manuscripts/{manuscript_id}/scenes/{scene_id}")
async def manuscripts_update_scene(
    manuscript_id: str,
    scene_id: str,
    body: SceneUpdate,
    user_id: str = Depends(get_current_user_id),
):
    ms = await db.get_manuscript(manuscript_id, user_id)
    if ms is None:
        raise HTTPException(status_code=404, detail="Manuscript not found")
    scene = await db.get_scene(scene_id)
    if scene is None or str(scene["manuscript_id"]) != manuscript_id:
        raise HTTPException(status_code=404, detail="Scene not found")
    row = await db.update_scene_content(scene_id, body.content)
    assert row is not None
    return {
        "id": str(row["id"]),
        "word_count": row["word_count"],
        "updated_at": row["updated_at"].isoformat(),
    }


@router.get("/manuscripts/{manuscript_id}/chapters")
async def manuscripts_list_chapters(
    manuscript_id: str,
    user_id: str = Depends(get_current_user_id),
):
    ms = await db.get_manuscript(manuscript_id, user_id)
    if ms is None:
        raise HTTPException(status_code=404, detail="Manuscript not found")
    chapters = await db.list_chapters(manuscript_id)
    return [
        {"id": str(c["id"]), "title": c["title"], "sort_order": c["sort_order"]}
        for c in chapters
    ]


@router.post("/author/brainstorm")
async def author_brainstorm(
    body: BrainstormRequest,
    user_id: str = Depends(get_current_user_id),
):
    ms = await db.get_manuscript(body.manuscript_id, user_id)
    if ms is None:
        raise HTTPException(status_code=404, detail="Manuscript not found")

    context_bits: list[str] = [f"Manuscript title: {ms['title']}"]
    if body.linked_scene_id:
        scene = await db.get_scene(body.linked_scene_id)
        if scene and str(scene["manuscript_id"]) == body.manuscript_id:
            context_bits.append(f"Chapter: {scene.get('chapter_title')}")
            context_bits.append(f"Scene content:\n{scene.get('content') or ''}")

    client = _anthropic()
    system = (
        "You are Olivia in Author brainstorm mode. Pair on plot and character "
        "ideas. Use provided scene/chapter context when present. Keep replies "
        "spoken-friendly and concise. Do not invent that text was saved."
    )

    def _call():
        return client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system + "\n\n" + "\n\n".join(context_bits),
            messages=[{"role": "user", "content": body.transcript}],
        )

    msg = await asyncio.to_thread(_call)
    reply = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    session = await db.create_brainstorm_session(
        body.manuscript_id, body.transcript, body.linked_scene_id
    )
    return {
        "reply": reply,
        "brainstorm_session_id": str(session["id"]),
        "pending_action": None,
        "visual_panel": None,
    }


# ---------------------------------------------------------------------------
# Wearables (Terra)
# ---------------------------------------------------------------------------

class WearablesConnectRequest(BaseModel):
    providers: Optional[list[str]] = None
    success_redirect_url: Optional[str] = None
    failure_redirect_url: Optional[str] = None


@router.post("/wearables/connect")
async def wearables_connect(
    body: WearablesConnectRequest,
    user_id: str = Depends(get_current_user_id),
):
    session = await terra.create_widget_session(
        reference_id=user_id,
        providers=body.providers,
        auth_success_redirect_url=body.success_redirect_url,
        auth_failure_redirect_url=body.failure_redirect_url,
    )
    # Persist a placeholder connection row keyed by aggregator; provider filled on webhook.
    await db.upsert_wearable_connection(user_id, provider="terra", aggregator_token_ref=None)
    return {
        "url": session.get("url") or session.get("session_id"),
        "session": session,
    }


@router.post("/wearables/terra/webhook")
async def wearables_terra_webhook(
    request: Request,
    terra_signature: Optional[str] = Header(None, alias="terra-signature"),
):
    body_bytes = await request.body()
    if not terra.verify_webhook_signature(body_bytes, terra_signature):
        raise HTTPException(status_code=401, detail="Invalid Terra webhook signature")

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    user = payload.get("user") or {}
    # Terra reference_id is the LifeSight user_id we passed at connect time.
    user_id = user.get("reference_id") or payload.get("reference_id")
    if not user_id:
        # Ack without write — some Terra pings are non-user events.
        return {"ok": True, "written": 0}

    provider = user.get("provider") or "terra"
    await db.upsert_wearable_connection(
        str(user_id),
        provider=str(provider),
        aggregator_token_ref=str(user.get("user_id") or "") or None,
    )

    written = 0
    for metric in terra.extract_metrics(payload):
        recorded_raw = metric.get("recorded_at")
        if recorded_raw:
            try:
                recorded_at = datetime.fromisoformat(str(recorded_raw).replace("Z", "+00:00"))
            except ValueError:
                recorded_at = datetime.now(timezone.utc)
        else:
            recorded_at = datetime.now(timezone.utc)
        await db.insert_health_metric(
            str(user_id),
            metric_type=metric["metric_type"],
            value=metric.get("value"),
            value_json=metric.get("value_json"),
            source_device=metric.get("source_device"),
            recorded_at=recorded_at,
        )
        written += 1
    return {"ok": True, "written": written}
