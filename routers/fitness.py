"""Fitness workout HTTP contract — plans, sessions, sets, PRs, history.

Identity always via Depends(get_current_user_id). Ownership is never accepted
from the request body. Missing, cross-user, and malformed ids → 404.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from shared.auth import get_current_user_id
from shared.fitness import service, store
from routers.fitness_models import (
    AdherenceOut,
    ExerciseHistoryOut,
    HistoryOut,
    LogSetOut,
    PersonalRecordsOut,
    PlanListOut,
    SessionDetailOut,
    StartSessionOut,
    VoiceLogOut,
    WorkoutPlanOut,
    WorkoutSessionStateOut,
)

router = APIRouter(tags=["fitness"])


class PlanExerciseIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=store.MAX_EXERCISE_NAME_CHARS)
    target_sets: Optional[int] = Field(default=None, ge=1, le=30)
    target_reps: Optional[int] = Field(default=None, ge=1, le=500)
    rest_seconds: Optional[int] = Field(default=None, ge=0, le=3600)
    notes: Optional[str] = Field(default=None, max_length=store.MAX_NOTES_CHARS)
    sort_order: Optional[int] = Field(default=None, ge=0, le=100)


class PlanDayIn(BaseModel):
    title: Optional[str] = Field(default=None, max_length=store.MAX_TITLE_CHARS)
    sort_order: Optional[int] = Field(default=None, ge=0, le=30)
    exercises: list[PlanExerciseIn] = Field(..., min_length=1)


class PlanCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=store.MAX_TITLE_CHARS)
    notes: Optional[str] = Field(default=None, max_length=store.MAX_NOTES_CHARS)
    days: list[PlanDayIn] = Field(..., min_length=1)
    activate: bool = True
    source_upload_ref: Optional[str] = Field(default=None, max_length=240)


class StartSessionRequest(BaseModel):
    plan_day_id: Optional[str] = None


class LogSetRequest(BaseModel):
    exercise_id: Optional[str] = None
    exercise_name: Optional[str] = None
    set_number: Optional[int] = Field(default=None, ge=1, le=store.MAX_SET_NUMBER)
    reps: Optional[int] = Field(default=None, ge=0, le=store.MAX_REPS)
    weight: Optional[float] = Field(default=None, ge=0, le=store.MAX_WEIGHT)
    source: Optional[str] = Field(default="manual")


class VoiceLogRequest(BaseModel):
    session_id: str
    transcript: str = Field(..., min_length=1)


@router.post("/workouts/plans", response_model=WorkoutPlanOut)
async def create_plan(
    body: PlanCreate,
    user_id: str = Depends(get_current_user_id),
):
    return await service.create_plan(user_id, body.model_dump())


@router.get("/workouts/plans/current", response_model=WorkoutPlanOut)
async def current_plan(user_id: str = Depends(get_current_user_id)):
    plan = await service.get_current_plan(user_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=service.NOT_FOUND_PLAN)
    return plan


@router.get("/workouts/plans", response_model=PlanListOut)
async def list_plans(
    limit: int = Query(default=20, ge=1, le=store.MAX_HISTORY_LIMIT),
    user_id: str = Depends(get_current_user_id),
):
    rows = await store.list_plans(user_id, limit=limit)
    return {"plans": [store.serialize_plan(r) for r in rows]}


@router.get("/workouts/plans/{plan_id}", response_model=WorkoutPlanOut)
async def get_plan(plan_id: str, user_id: str = Depends(get_current_user_id)):
    return await service.get_plan_detail(plan_id, user_id)


@router.post("/workouts/plans/{plan_id}/activate", response_model=WorkoutPlanOut)
async def activate_plan(plan_id: str, user_id: str = Depends(get_current_user_id)):
    return await service.activate_plan(plan_id, user_id)


@router.post("/workouts/session/start", response_model=StartSessionOut)
async def workouts_session_start(
    body: StartSessionRequest,
    user_id: str = Depends(get_current_user_id),
):
    return await service.start_workout(user_id, body.plan_day_id)


@router.get("/workouts/session/active", response_model=WorkoutSessionStateOut)
async def workouts_session_active(user_id: str = Depends(get_current_user_id)):
    active = await service.get_active_workout(user_id)
    if active is None:
        raise HTTPException(status_code=404, detail=service.NOT_FOUND_SESSION)
    return active


@router.get("/workouts/session/{session_id}/state", response_model=WorkoutSessionStateOut)
async def workouts_session_state(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    return await service.get_session_state(session_id, user_id)


@router.get("/workouts/session/{session_id}", response_model=SessionDetailOut)
async def workouts_session_detail(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    return await service.get_session_detail(session_id, user_id)


@router.post("/workouts/session/{session_id}/sets", response_model=LogSetOut)
async def workouts_log_set(
    session_id: str,
    body: LogSetRequest,
    user_id: str = Depends(get_current_user_id),
):
    result = await service.log_set(
        user_id,
        session_id,
        exercise_id=body.exercise_id,
        exercise_name=body.exercise_name,
        set_number=body.set_number,
        reps=body.reps,
        weight=body.weight,
        source=body.source or "manual",
    )
    return {
        "set": result["set"],
        "is_new_pr": result["is_new_pr"],
        "pr_announcement": result["pr_announcement"],
        "reply": result["pr_announcement"] or "Set logged.",
        "pending_action": None,
        "visual_panel": result["visual_panel"],
        "state": result["state"],
    }


@router.post("/workouts/session/{session_id}/complete", response_model=WorkoutSessionStateOut)
async def workouts_complete(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    return await service.complete_workout(session_id, user_id)


@router.post("/workouts/session/{session_id}/abandon", response_model=WorkoutSessionStateOut)
async def workouts_abandon(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    return await service.abandon_workout(session_id, user_id)


@router.get("/workouts/history", response_model=HistoryOut)
async def workouts_history(
    limit: int = Query(default=store.DEFAULT_HISTORY_LIMIT, ge=1, le=store.MAX_HISTORY_LIMIT),
    before: Optional[datetime] = None,
    user_id: str = Depends(get_current_user_id),
):
    return await service.list_history(user_id, limit=limit, before=before)


@router.get("/workouts/exercises/{exercise_id}/history", response_model=ExerciseHistoryOut)
async def workouts_exercise_history(
    exercise_id: str,
    limit: int = Query(default=store.DEFAULT_HISTORY_LIMIT, ge=1, le=store.MAX_HISTORY_LIMIT),
    user_id: str = Depends(get_current_user_id),
):
    return await service.exercise_history(exercise_id, user_id, limit=limit)


@router.get("/workouts/personal-records", response_model=PersonalRecordsOut)
async def workouts_prs(
    limit: int = Query(default=store.DEFAULT_PR_LIMIT, ge=1, le=store.MAX_PR_LIMIT),
    user_id: str = Depends(get_current_user_id),
):
    return await service.list_prs(user_id, limit=limit)


@router.get("/workouts/adherence", response_model=AdherenceOut)
async def workouts_adherence(user_id: str = Depends(get_current_user_id)):
    return await service.adherence(user_id)


@router.post("/workouts/voice-log", response_model=VoiceLogOut)
async def workouts_voice_log(
    body: VoiceLogRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Preserve the v2 voice-log shape; sets still go through the same engine."""
    from routers import v2 as v2_mod

    session = await store.get_session(body.session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail=service.NOT_FOUND_SESSION)
    if session["status"] != "active":
        raise HTTPException(status_code=409, detail=service.NOT_ACTIVE)
    if not session.get("plan_day_id"):
        raise HTTPException(status_code=400, detail=service.NO_PLAN_DAY)
    exercises = await store.list_exercises_for_day(str(session["plan_day_id"]), user_id)
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
    parsed = await v2_mod._parse_voice_sets(body.transcript, catalog)
    if not parsed:
        raise HTTPException(
            status_code=422,
            detail="Could not parse any sets from that utterance. Try saying reps and weight.",
        )
    logged = []
    pr_announcements: list[str] = []
    visual_panel = None
    state = None
    for item in parsed:
        result = await service.log_set(
            user_id,
            body.session_id,
            exercise_id=item.get("exercise_id"),
            set_number=item.get("set_number"),
            reps=item.get("reps"),
            weight=item.get("weight"),
            source="voice",
        )
        logged.append(result["set"])
        if result["pr_announcement"]:
            pr_announcements.append(result["pr_announcement"])
        visual_panel = {
            "type": "workout_sets",
            "data": {"session_id": body.session_id, "sets": logged},
        }
        state = result["state"]
    return {
        "sets": logged,
        "pr_announcements": pr_announcements,
        "reply": (
            (" ".join(pr_announcements) + " " if pr_announcements else "")
            + f"Logged {len(logged)} set{'s' if len(logged) != 1 else ''}."
        ).strip(),
        "visual_panel": visual_panel,
        "pending_action": None,
        "state": state,
    }
