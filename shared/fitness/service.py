"""Fitness workout orchestration — HTTPException at this layer, thin routers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException

from shared.fitness import progress, store
from shared.ids import normalized_uuid
from shared.visual_panels import ExercisePanelData, exercise_visual_panel

NOT_FOUND_SESSION = "Workout session not found"
NOT_FOUND_PLAN = "Workout plan not found"
NOT_FOUND_DAY = "Workout day not found"
NOT_FOUND_EXERCISE = "Exercise not found"
NOT_ACTIVE = "Workout session is not active"
ACTIVE_CONFLICT = (
    "An active workout already exists. Complete or abandon it before starting "
    "a different day."
)
NO_PLAN_DAY = "Session has no plan day — cannot match exercises"
DUPLICATE_SET = "That set number is already logged for this exercise"


def _require_uuid(value: Optional[str], detail: str) -> str:
    canonical = normalized_uuid(value)
    if canonical is None:
        raise HTTPException(status_code=404, detail=detail)
    return canonical


async def next_plan_day_id(user_id: str) -> Optional[str]:
    """Next unused day on the active plan, wrapping to the first day."""
    plan = await store.get_active_plan(user_id)
    if plan is None:
        return None
    days = await store.list_days_for_plan(str(plan["id"]), user_id)
    if not days:
        return None
    sessions = await store.list_sessions(user_id, limit=store.MAX_HISTORY_LIMIT)
    completed_days: list[str] = []
    for sess in sessions:
        if sess["status"] != "completed" or not sess.get("plan_day_id"):
            continue
        completed_days.append(str(sess["plan_day_id"]))
    order = [str(d["id"]) for d in days]
    if not completed_days:
        return order[0]
    last = None
    for day_id in completed_days:
        if day_id in order:
            last = day_id
            break
    if last is None:
        return order[0]
    idx = order.index(last)
    return order[(idx + 1) % len(order)]


def serialize_state(session: dict, prog: dict[str, Any]) -> dict:
    current = prog["current_exercise"]
    return {
        "session_id": str(session["id"]),
        "status": session["status"],
        "session_date": store._iso(session.get("session_date")),
        "plan_day_id": str(session["plan_day_id"]) if session.get("plan_day_id") else None,
        "started_at": store._iso(session.get("started_at")),
        "ended_at": store._iso(session.get("ended_at")),
        "current_exercise": (
            {
                "id": str(current["id"]),
                "name": current["name"],
                "target_sets": current.get("target_sets"),
                "target_reps": current.get("target_reps"),
                "rest_seconds": current.get("rest_seconds"),
                "notes": current.get("notes"),
            }
            if current
            else None
        ),
        "current_set_number": prog["current_set_number"],
        "rest_seconds": current.get("rest_seconds") if current else None,
        "sets_logged": prog["sets_logged"],
        "remaining_sets_on_current": prog["remaining_sets_on_current"],
    }


def exercise_panel_from_progress(prog: dict[str, Any]) -> Optional[dict]:
    current = prog.get("current_exercise")
    if current is None:
        return None
    data = ExercisePanelData(
        exercise_id=str(current["id"]),
        exercise_name=current["name"],
        sets=int(current["target_sets"] or 1),
        reps=int(current["target_reps"] or 1),
        rest_seconds=int(current["rest_seconds"] or 0),
        current_set=int(prog["current_set_number"]),
        notes=current.get("notes"),
    )
    return exercise_visual_panel(data).model_dump(mode="json")


async def overlay_exercise_panel(
    user_id: str, data: ExercisePanelData
) -> ExercisePanelData:
    """When an active session exists, current_set/prescription come from it."""
    session = await store.get_active_session(user_id)
    if session is None:
        return data
    prog = await progress.session_progress(session, user_id)
    current = prog["current_exercise"]
    if current is None:
        return data
    current_id = str(current["id"])
    if data.exercise_id and data.exercise_id != current_id:
        named = None
        for ex in prog["exercises"]:
            if str(ex["id"]) == data.exercise_id:
                named = ex
                break
        if named is None:
            return data
        done = len(prog["logs_by_exercise"].get(str(named["id"]), []))
        return data.model_copy(
            update={
                "exercise_id": str(named["id"]),
                "exercise_name": named["name"],
                "sets": int(named["target_sets"] or data.sets),
                "reps": int(named["target_reps"] or data.reps),
                "rest_seconds": int(
                    named["rest_seconds"]
                    if named.get("rest_seconds") is not None
                    else data.rest_seconds
                ),
                "current_set": done + 1,
                "notes": named.get("notes") if named.get("notes") else data.notes,
            }
        )
    if data.exercise_id is None:
        name = (data.exercise_name or "").strip().lower()
        if name and name == str(current.get("name") or "").strip().lower():
            data = data.model_copy(update={"exercise_id": current_id})
        elif data.exercise_id is None and name:
            return data
    return data.model_copy(
        update={
            "exercise_id": current_id,
            "exercise_name": current["name"],
            "sets": int(current["target_sets"] or data.sets),
            "reps": int(current["target_reps"] or data.reps),
            "rest_seconds": int(
                current["rest_seconds"]
                if current.get("rest_seconds") is not None
                else data.rest_seconds
            ),
            "current_set": int(prog["current_set_number"]),
            "notes": current.get("notes") if current.get("notes") else data.notes,
        }
    )


async def create_plan(user_id: str, body: dict) -> dict:
    days_in = body.get("days") or []
    if not days_in:
        raise HTTPException(status_code=400, detail="A plan needs at least one day")
    if len(days_in) > store.MAX_PLAN_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"A plan may have at most {store.MAX_PLAN_DAYS} days",
        )
    normalized_days: list[dict] = []
    for index, day in enumerate(days_in):
        exercises_in = day.get("exercises") or []
        if not exercises_in:
            raise HTTPException(
                status_code=400, detail="Each workout day needs at least one exercise"
            )
        if len(exercises_in) > store.MAX_EXERCISES_PER_DAY:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"A workout day may have at most {store.MAX_EXERCISES_PER_DAY} "
                    "exercises"
                ),
            )
        exercises = []
        for ex_index, ex in enumerate(exercises_in):
            name = str(ex.get("name") or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="Exercise name is required")
            if len(name) > store.MAX_EXERCISE_NAME_CHARS:
                raise HTTPException(status_code=400, detail="Exercise name is too long")
            exercises.append(
                {
                    "name": name,
                    "target_sets": ex.get("target_sets"),
                    "target_reps": ex.get("target_reps"),
                    "rest_seconds": ex.get("rest_seconds"),
                    "sort_order": ex.get("sort_order", ex_index),
                    "notes": ex.get("notes"),
                }
            )
        normalized_days.append(
            {
                "title": day.get("title"),
                "sort_order": day.get("sort_order", index),
                "exercises": exercises,
            }
        )
    title = (body.get("title") or None)
    if title is not None:
        title = str(title).strip() or None
        if title and len(title) > store.MAX_TITLE_CHARS:
            raise HTTPException(status_code=400, detail="Plan title is too long")
    notes = body.get("notes")
    if notes is not None:
        notes = str(notes).strip() or None
        if notes and len(notes) > store.MAX_NOTES_CHARS:
            raise HTTPException(status_code=400, detail="Plan notes are too long")
    row = await store.create_plan(
        user_id,
        title=title,
        notes=notes,
        days=normalized_days,
        activate=bool(body.get("activate", True)),
        source_upload_ref=body.get("source_upload_ref"),
    )
    return await store.assemble_plan(str(row["id"]), user_id)


async def get_plan_detail(plan_id: str, user_id: str) -> dict:
    _require_uuid(plan_id, NOT_FOUND_PLAN)
    assembled = await store.assemble_plan(plan_id, user_id)
    if assembled is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_PLAN)
    return assembled


async def get_current_plan(user_id: str) -> Optional[dict]:
    plan = await store.get_active_plan(user_id)
    if plan is None:
        return None
    return await store.assemble_plan(str(plan["id"]), user_id)


async def activate_plan(plan_id: str, user_id: str) -> dict:
    _require_uuid(plan_id, NOT_FOUND_PLAN)
    row = await store.activate_plan(plan_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_PLAN)
    return await store.assemble_plan(plan_id, user_id)


async def start_workout(user_id: str, plan_day_id: Optional[str] = None) -> dict:
    requested = plan_day_id
    if requested:
        requested = _require_uuid(requested, NOT_FOUND_DAY)
        if await store.get_day(requested, user_id) is None:
            raise HTTPException(status_code=404, detail=NOT_FOUND_DAY)
    elif requested is None:
        requested = await next_plan_day_id(user_id)
    try:
        session, resumed = await store.start_or_resume_session(user_id, requested)
    except store.ActiveSessionConflict:
        raise HTTPException(status_code=409, detail=ACTIVE_CONFLICT)
    if session is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DAY)
    prog = await progress.session_progress(session, user_id)
    payload = serialize_state(session, prog)
    payload["resumed"] = resumed
    return payload


async def get_active_workout(user_id: str) -> Optional[dict]:
    session = await store.get_active_session(user_id)
    if session is None:
        return None
    prog = await progress.session_progress(session, user_id)
    return serialize_state(session, prog)


async def get_session_state(session_id: str, user_id: str) -> dict:
    _require_uuid(session_id, NOT_FOUND_SESSION)
    session = await store.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_SESSION)
    prog = await progress.session_progress(session, user_id)
    return serialize_state(session, prog)


async def get_session_detail(session_id: str, user_id: str) -> dict:
    state = await get_session_state(session_id, user_id)
    session = await store.get_session(session_id, user_id)
    assert session is not None
    prog = await progress.session_progress(session, user_id)
    names = {str(ex["id"]): ex["name"] for ex in prog["exercises"]}
    state["sets"] = [
        store.serialize_set(lg, exercise_name=names.get(str(lg["exercise_id"])))
        for lg in prog["logs"]
    ]
    state["exercises"] = [store.serialize_exercise(ex) for ex in prog["exercises"]]
    return state


async def log_set(
    user_id: str,
    session_id: str,
    *,
    exercise_id: Optional[str] = None,
    exercise_name: Optional[str] = None,
    set_number: Optional[int] = None,
    reps: Optional[int] = None,
    weight: Optional[float] = None,
    source: str = "manual",
) -> dict:
    _require_uuid(session_id, NOT_FOUND_SESSION)
    session = await store.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_SESSION)
    if session["status"] != "active":
        raise HTTPException(status_code=409, detail=NOT_ACTIVE)
    if not session.get("plan_day_id"):
        raise HTTPException(status_code=400, detail=NO_PLAN_DAY)
    prog = await progress.session_progress(session, user_id)
    target = None
    if exercise_id:
        exercise_id = _require_uuid(exercise_id, NOT_FOUND_EXERCISE)
        target = progress.exercise_in_session(prog, exercise_id)
        if target is None:
            raise HTTPException(status_code=404, detail=NOT_FOUND_EXERCISE)
    elif exercise_name:
        needle = exercise_name.strip().lower()
        matches = [
            ex
            for ex in prog["exercises"]
            if str(ex["name"]).strip().lower() == needle
        ]
        if len(matches) != 1:
            raise HTTPException(
                status_code=422,
                detail="Could not uniquely match that exercise on today's plan",
            )
        target = matches[0]
        exercise_id = str(target["id"])
    else:
        current = prog["current_exercise"]
        if current is None:
            raise HTTPException(status_code=400, detail=NOT_FOUND_EXERCISE)
        target = current
        exercise_id = str(current["id"])

    if set_number is None:
        set_number = progress.next_set_number(prog, exercise_id)
    if set_number < 1 or set_number > store.MAX_SET_NUMBER:
        raise HTTPException(status_code=400, detail="set_number is out of range")
    if reps is not None and (reps < 0 or reps > store.MAX_REPS):
        raise HTTPException(status_code=400, detail="reps is out of range")
    if weight is not None and (weight < 0 or weight > store.MAX_WEIGHT):
        raise HTTPException(status_code=400, detail="weight is out of range")
    if source not in store.SET_SOURCES:
        source = "manual"

    try:
        row = await store.insert_set_log(
            user_id,
            session_id,
            exercise_id,
            set_number,
            reps,
            weight,
            source=source,
        )
    except store.DuplicateSetError:
        raise HTTPException(status_code=409, detail=DUPLICATE_SET)

    pr_announcement = None
    is_new_pr = False
    if reps is not None and reps >= 1 and weight is not None and weight > 0:
        _pr_row, is_new_pr = await store.upsert_personal_record(
            user_id, exercise_id, int(reps), float(weight)
        )
        if is_new_pr:
            pr_announcement = (
                f"New personal record: {target['name']}, {reps} reps at {weight}."
            )

    session = await store.get_session(session_id, user_id)
    assert session is not None
    new_prog = await progress.session_progress(session, user_id)
    return {
        "set": store.serialize_set(row, exercise_name=target["name"]),
        "pr_announcement": pr_announcement,
        "is_new_pr": is_new_pr,
        "state": serialize_state(session, new_prog),
        "visual_panel": exercise_panel_from_progress(new_prog),
    }


async def complete_workout(session_id: str, user_id: str) -> dict:
    _require_uuid(session_id, NOT_FOUND_SESSION)
    session = await store.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_SESSION)
    if session["status"] != "active":
        raise HTTPException(status_code=409, detail=NOT_ACTIVE)
    updated = await store.complete_session(session_id, user_id)
    assert updated is not None
    prog = await progress.session_progress(updated, user_id)
    return serialize_state(updated, prog)


async def abandon_workout(session_id: str, user_id: str) -> dict:
    _require_uuid(session_id, NOT_FOUND_SESSION)
    session = await store.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_SESSION)
    if session["status"] != "active":
        raise HTTPException(status_code=409, detail=NOT_ACTIVE)
    updated = await store.abandon_session(session_id, user_id)
    assert updated is not None
    prog = await progress.session_progress(updated, user_id)
    return serialize_state(updated, prog)


async def list_history(
    user_id: str,
    *,
    limit: Optional[int] = None,
    before: Optional[datetime] = None,
) -> dict:
    rows = await store.list_sessions(user_id, limit=limit or store.DEFAULT_HISTORY_LIMIT, before=before)
    return {
        "sessions": [store.serialize_session(r) for r in rows],
        "limit": store.clamp_limit(
            limit, store.DEFAULT_HISTORY_LIMIT, store.MAX_HISTORY_LIMIT
        ),
    }


async def exercise_history(exercise_id: str, user_id: str, *, limit: Optional[int] = None) -> dict:
    _require_uuid(exercise_id, NOT_FOUND_EXERCISE)
    exercise = await store.get_exercise(exercise_id, user_id)
    if exercise is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_EXERCISE)
    rows = await store.list_exercise_history(
        user_id, exercise_id, limit=limit or store.DEFAULT_HISTORY_LIMIT
    )
    return {
        "exercise": store.serialize_exercise(exercise),
        "sets": [store.serialize_set(r, exercise_name=exercise["name"]) for r in rows],
        "weight_unit": None,
        "weight_unit_note": (
            "Weight is a unitless number. Display using the profile "
            "preferred_units hint; the backend does not store lb vs kg on sets."
        ),
    }


async def list_prs(user_id: str, *, limit: Optional[int] = None) -> dict:
    rows = await store.list_personal_records(user_id, limit=limit or store.DEFAULT_PR_LIMIT)
    out = []
    for row in rows:
        out.append(
            store.serialize_pr(row, exercise_name=row.get("exercise_name"))
        )
    return {
        "personal_records": out,
        "weight_unit": None,
        "weight_unit_note": (
            "PRs are stored by rep_range. A best 5-rep set never overwrites a "
            "best 1-rep set. Weight is unitless."
        ),
    }


async def adherence(user_id: str) -> dict:
    raw = await store.adherence_counts(user_id, window_days=28)
    last = raw["last_workout"]
    return {
        "sessions_completed": raw["sessions_completed"],
        "sessions_abandoned": raw["sessions_abandoned"],
        "sets_completed": raw["sets_completed"],
        "last_workout": store.serialize_session(last) if last else None,
        "recent_frequency": {
            "window_days": raw["window_days"],
            "sessions_completed": raw["recent_completed_in_window"],
        },
    }
