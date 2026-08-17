"""Fitness workout storage — Postgres via shared.db.pool, memory for tests.

Ownership: every query scopes by the JWT user_id. Request bodies never supply
ownership. Nested rows (days, exercises, sets) also carry user_id and are
joined so a child cannot attach to another user's parent.

Malformed ids are normalized first so they never reach `$1::uuid`.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import asyncpg

from shared import db
from shared.ids import normalized_uuid

SESSION_STATUSES = ("active", "completed", "abandoned")
SET_SOURCES = ("voice", "manual")

MAX_PLAN_DAYS = 7
MAX_EXERCISES_PER_DAY = 16
MAX_TITLE_CHARS = 120
MAX_NOTES_CHARS = 500
MAX_EXERCISE_NAME_CHARS = 120
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 50
DEFAULT_PR_LIMIT = 50
MAX_PR_LIMIT = 100
MAX_SET_NUMBER = 30
MAX_REPS = 500
MAX_WEIGHT = 2000.0

# Check-in / health tool windows
DEFAULT_CHECKIN_DAYS = 7
MAX_CHECKIN_DAYS = 14
DEFAULT_CHECKIN_LIMIT = 14


class DuplicateSetError(Exception):
    """(session, exercise, set_number) already exists."""


class ActiveSessionConflict(Exception):
    """Caller asked to start a different day while one session is already active."""


@dataclass
class _MemoryStore:
    plans: dict[str, dict] = field(default_factory=dict)
    days: dict[str, dict] = field(default_factory=dict)
    exercises: dict[str, dict] = field(default_factory=dict)
    sessions: dict[str, dict] = field(default_factory=dict)
    sets: dict[str, dict] = field(default_factory=dict)
    prs: dict[tuple[str, str, int], dict] = field(default_factory=dict)
    checkins: list[dict] = field(default_factory=list)


_memory: Optional[_MemoryStore] = None


def use_memory_store(enabled: bool = True) -> _MemoryStore:
    global _memory
    if enabled:
        _memory = _MemoryStore()
        return _memory
    _memory = None
    return _MemoryStore()


def _mem() -> Optional[_MemoryStore]:
    return _memory


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def clamp_limit(limit: Optional[int], default: int, maximum: int) -> int:
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return 1
    return min(value, maximum)


def serialize_plan(row: dict, *, days: Optional[list[dict]] = None) -> dict:
    payload = {
        "id": str(row["id"]),
        "title": row.get("title"),
        "notes": row.get("notes"),
        "source_upload_ref": row.get("source_upload_ref"),
        "is_active": bool(row.get("is_active")),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }
    if days is not None:
        payload["days"] = days
    return payload


def serialize_day(row: dict, *, exercises: Optional[list[dict]] = None) -> dict:
    payload = {
        "id": str(row["id"]),
        "plan_id": str(row["plan_id"]),
        "sort_order": int(row["sort_order"]),
        "title": row.get("title"),
    }
    if exercises is not None:
        payload["exercises"] = exercises
    return payload


def serialize_exercise(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "day_id": str(row["day_id"]),
        "name": row["name"],
        "target_sets": row.get("target_sets"),
        "target_reps": row.get("target_reps"),
        "rest_seconds": row.get("rest_seconds"),
        "sort_order": int(row["sort_order"]),
        "notes": row.get("notes"),
    }


def serialize_session(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "session_date": _iso(row.get("session_date")),
        "plan_day_id": str(row["plan_day_id"]) if row.get("plan_day_id") else None,
        "status": row["status"],
        "started_at": _iso(row.get("started_at")),
        "ended_at": _iso(row.get("ended_at")),
    }


def serialize_set(row: dict, *, exercise_name: Optional[str] = None) -> dict:
    payload = {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "exercise_id": str(row["exercise_id"]),
        "set_number": int(row["set_number"]),
        "reps": row.get("reps"),
        "weight": float(row["weight"]) if row.get("weight") is not None else None,
        "source": row.get("source") or "voice",
        "completed_at": _iso(row.get("completed_at")),
    }
    if exercise_name is not None:
        payload["exercise_name"] = exercise_name
    return payload


def serialize_pr(row: dict, *, exercise_name: Optional[str] = None) -> dict:
    payload = {
        "id": str(row["id"]),
        "exercise_id": str(row["exercise_id"]),
        "rep_range": int(row["rep_range"]),
        "weight": float(row["weight"]),
        "achieved_at": _iso(row.get("achieved_at")),
    }
    if exercise_name is not None:
        payload["exercise_name"] = exercise_name
    return payload


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

async def get_plan(plan_id: str, user_id: str) -> Optional[dict]:
    plan_id = normalized_uuid(plan_id)
    if plan_id is None:
        return None
    if _mem() is not None:
        row = _mem().plans.get(plan_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)
    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, source_upload_ref, title, notes, is_active,
               created_at, updated_at
        FROM workout_plans
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        plan_id,
        user_id,
    )
    return dict(row) if row else None


async def get_active_plan(user_id: str) -> Optional[dict]:
    if _mem() is not None:
        matches = [p for p in _mem().plans.values() if p["user_id"] == user_id and p["is_active"]]
        return deepcopy(matches[0]) if matches else None
    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, source_upload_ref, title, notes, is_active,
               created_at, updated_at
        FROM workout_plans
        WHERE user_id = $1::uuid AND is_active
        LIMIT 1
        """,
        user_id,
    )
    return dict(row) if row else None


async def list_plans(user_id: str, *, limit: int = 20) -> list[dict]:
    limit = clamp_limit(limit, 20, MAX_HISTORY_LIMIT)
    if _mem() is not None:
        rows = [p for p in _mem().plans.values() if p["user_id"] == user_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return deepcopy(rows[:limit])
    rows = await db.pool().fetch(
        """
        SELECT id, user_id, source_upload_ref, title, notes, is_active,
               created_at, updated_at
        FROM workout_plans
        WHERE user_id = $1::uuid
        ORDER BY is_active DESC, created_at DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def list_days_for_plan(plan_id: str, user_id: str) -> list[dict]:
    plan_id = normalized_uuid(plan_id)
    if plan_id is None:
        return []
    if _mem() is not None:
        rows = [
            d
            for d in _mem().days.values()
            if d["plan_id"] == plan_id and d["user_id"] == user_id
        ]
        rows.sort(key=lambda r: r["sort_order"])
        return deepcopy(rows)
    rows = await db.pool().fetch(
        """
        SELECT id, user_id, plan_id, sort_order, title
        FROM workout_days
        WHERE plan_id = $1::uuid AND user_id = $2::uuid
        ORDER BY sort_order
        """,
        plan_id,
        user_id,
    )
    return [dict(r) for r in rows]


async def list_exercises_for_day(day_id: str, user_id: str) -> list[dict]:
    day_id = normalized_uuid(day_id)
    if day_id is None:
        return []
    if _mem() is not None:
        rows = [
            e
            for e in _mem().exercises.values()
            if e["day_id"] == day_id and e["user_id"] == user_id
        ]
        rows.sort(key=lambda r: r["sort_order"])
        return deepcopy(rows)
    rows = await db.pool().fetch(
        """
        SELECT id, user_id, day_id, name, target_sets, target_reps,
               rest_seconds, sort_order, notes
        FROM planned_exercises
        WHERE day_id = $1::uuid AND user_id = $2::uuid
        ORDER BY sort_order
        """,
        day_id,
        user_id,
    )
    return [dict(r) for r in rows]


async def get_day(day_id: str, user_id: str) -> Optional[dict]:
    day_id = normalized_uuid(day_id)
    if day_id is None:
        return None
    if _mem() is not None:
        row = _mem().days.get(day_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)
    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, plan_id, sort_order, title
        FROM workout_days
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        day_id,
        user_id,
    )
    return dict(row) if row else None


async def get_exercise(exercise_id: str, user_id: str) -> Optional[dict]:
    exercise_id = normalized_uuid(exercise_id)
    if exercise_id is None:
        return None
    if _mem() is not None:
        row = _mem().exercises.get(exercise_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)
    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, day_id, name, target_sets, target_reps,
               rest_seconds, sort_order, notes
        FROM planned_exercises
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        exercise_id,
        user_id,
    )
    return dict(row) if row else None


async def create_plan(
    user_id: str,
    *,
    title: Optional[str],
    notes: Optional[str],
    days: list[dict],
    activate: bool = True,
    source_upload_ref: Optional[str] = None,
) -> dict:
    """Insert a plan with nested days/exercises. Optionally activate it."""
    now = _now()
    plan_id = str(uuid.uuid4())
    plan_row = {
        "id": plan_id,
        "user_id": user_id,
        "source_upload_ref": source_upload_ref,
        "title": title,
        "notes": notes,
        "is_active": False,
        "created_at": now,
        "updated_at": now,
    }
    created_days: list[dict] = []
    created_exercises: list[dict] = []
    for day in days:
        day_id = str(uuid.uuid4())
        created_days.append(
            {
                "id": day_id,
                "user_id": user_id,
                "plan_id": plan_id,
                "sort_order": int(day["sort_order"]),
                "title": day.get("title"),
            }
        )
        for ex in day["exercises"]:
            created_exercises.append(
                {
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "day_id": day_id,
                    "name": ex["name"],
                    "target_sets": ex.get("target_sets"),
                    "target_reps": ex.get("target_reps"),
                    "rest_seconds": ex.get("rest_seconds"),
                    "sort_order": int(ex["sort_order"]),
                    "notes": ex.get("notes"),
                }
            )

    if _mem() is not None:
        _mem().plans[plan_id] = plan_row
        for d in created_days:
            _mem().days[d["id"]] = d
        for e in created_exercises:
            _mem().exercises[e["id"]] = e
        if activate:
            await activate_plan(plan_id, user_id)
        return (await get_plan(plan_id, user_id)) or plan_row

    pool = db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO workout_plans (
                    id, user_id, source_upload_ref, title, notes, is_active
                )
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, FALSE)
                """,
                plan_id,
                user_id,
                source_upload_ref,
                title,
                notes,
            )
            for d in created_days:
                await conn.execute(
                    """
                    INSERT INTO workout_days (id, user_id, plan_id, sort_order, title)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5)
                    """,
                    d["id"],
                    user_id,
                    plan_id,
                    d["sort_order"],
                    d["title"],
                )
            for e in created_exercises:
                await conn.execute(
                    """
                    INSERT INTO planned_exercises (
                        id, user_id, day_id, name, target_sets, target_reps,
                        rest_seconds, sort_order, notes
                    )
                    VALUES (
                        $1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9
                    )
                    """,
                    e["id"],
                    user_id,
                    e["day_id"],
                    e["name"],
                    e["target_sets"],
                    e["target_reps"],
                    e["rest_seconds"],
                    e["sort_order"],
                    e["notes"],
                )
            if activate:
                await conn.execute(
                    """
                    UPDATE workout_plans
                    SET is_active = FALSE, updated_at = now()
                    WHERE user_id = $1::uuid AND is_active AND id <> $2::uuid
                    """,
                    user_id,
                    plan_id,
                )
                await conn.execute(
                    """
                    UPDATE workout_plans
                    SET is_active = TRUE, updated_at = now()
                    WHERE id = $1::uuid AND user_id = $2::uuid
                    """,
                    plan_id,
                    user_id,
                )
    return await get_plan(plan_id, user_id)


async def activate_plan(plan_id: str, user_id: str) -> Optional[dict]:
    plan = await get_plan(plan_id, user_id)
    if plan is None:
        return None
    if _mem() is not None:
        for row in _mem().plans.values():
            if row["user_id"] == user_id:
                row["is_active"] = row["id"] == plan_id
                row["updated_at"] = _now()
        return await get_plan(plan_id, user_id)
    await db.pool().execute(
        """
        UPDATE workout_plans
        SET is_active = (id = $1::uuid), updated_at = now()
        WHERE user_id = $2::uuid AND (is_active OR id = $1::uuid)
        """,
        plan_id,
        user_id,
    )
    return await get_plan(plan_id, user_id)


async def assemble_plan(plan_id: str, user_id: str) -> Optional[dict]:
    plan = await get_plan(plan_id, user_id)
    if plan is None:
        return None
    days_out: list[dict] = []
    for day in await list_days_for_plan(plan_id, user_id):
        exercises = [
            serialize_exercise(ex)
            for ex in await list_exercises_for_day(str(day["id"]), user_id)
        ]
        days_out.append(serialize_day(day, exercises=exercises))
    return serialize_plan(plan, days=days_out)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

async def get_session(session_id: str, user_id: str) -> Optional[dict]:
    session_id = normalized_uuid(session_id)
    if session_id is None:
        return None
    if _mem() is not None:
        row = _mem().sessions.get(session_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)
    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, session_date, plan_day_id, status, started_at, ended_at
        FROM workout_sessions
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        session_id,
        user_id,
    )
    return dict(row) if row else None


async def get_active_session(user_id: str) -> Optional[dict]:
    if _mem() is not None:
        matches = [
            s
            for s in _mem().sessions.values()
            if s["user_id"] == user_id and s["status"] == "active"
        ]
        return deepcopy(matches[0]) if matches else None
    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, session_date, plan_day_id, status, started_at, ended_at
        FROM workout_sessions
        WHERE user_id = $1::uuid AND status = 'active'
        LIMIT 1
        """,
        user_id,
    )
    return dict(row) if row else None


async def get_last_completed_plan_day_id(
    user_id: str, *, plan_day_ids: list[str]
) -> Optional[str]:
    """Most recently finished session on one of the given plan days, if any."""
    if not plan_day_ids:
        return None
    allowed = {normalized_uuid(day_id) for day_id in plan_day_ids}
    allowed.discard(None)
    if not allowed:
        return None
    if _mem() is not None:
        candidates = [
            s
            for s in _mem().sessions.values()
            if s["user_id"] == user_id
            and s["status"] == "completed"
            and s.get("plan_day_id") in allowed
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda r: r.get("ended_at") or r["started_at"], reverse=True
        )
        return str(candidates[0]["plan_day_id"])
    row = await db.pool().fetchrow(
        """
        SELECT plan_day_id
        FROM workout_sessions
        WHERE user_id = $1::uuid
          AND status = 'completed'
          AND plan_day_id = ANY($2::uuid[])
        ORDER BY COALESCE(ended_at, started_at) DESC
        LIMIT 1
        """,
        user_id,
        list(allowed),
    )
    return str(row["plan_day_id"]) if row else None


async def start_or_resume_session(
    user_id: str,
    plan_day_id: Optional[str] = None,
) -> tuple[dict, bool]:
    """Return (session, resumed).

    Resumes the active session when plan_day_id is omitted or matches.
    Raises ActiveSessionConflict when an active session exists for a different day.
    """
    if plan_day_id is not None:
        plan_day_id = normalized_uuid(plan_day_id)
        if plan_day_id is None:
            return None, False  # type: ignore[return-value]
        day = await get_day(plan_day_id, user_id)
        if day is None:
            return None, False  # type: ignore[return-value]

    active = await get_active_session(user_id)
    if active is not None:
        active_day = str(active["plan_day_id"]) if active.get("plan_day_id") else None
        if plan_day_id is None or plan_day_id == active_day:
            return active, True
        raise ActiveSessionConflict()

    now = _now()
    session_id = str(uuid.uuid4())
    row = {
        "id": session_id,
        "user_id": user_id,
        "session_date": now.date(),
        "plan_day_id": plan_day_id,
        "status": "active",
        "started_at": now,
        "ended_at": None,
    }
    if _mem() is not None:
        _mem().sessions[session_id] = row
        return deepcopy(row), False
    try:
        inserted = await db.pool().fetchrow(
            """
            INSERT INTO workout_sessions (id, user_id, plan_day_id, status)
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'active')
            RETURNING id, user_id, session_date, plan_day_id, status, started_at, ended_at
            """,
            session_id,
            user_id,
            plan_day_id,
        )
        return dict(inserted), False
    except asyncpg.UniqueViolationError:
        raced = await get_active_session(user_id)
        if raced is None:
            raise
        raced_day = str(raced["plan_day_id"]) if raced.get("plan_day_id") else None
        if plan_day_id is None or plan_day_id == raced_day:
            return raced, True
        raise ActiveSessionConflict()


async def complete_session(session_id: str, user_id: str) -> Optional[dict]:
    return await _set_session_status(session_id, user_id, "completed")


async def abandon_session(session_id: str, user_id: str) -> Optional[dict]:
    return await _set_session_status(session_id, user_id, "abandoned")


async def _set_session_status(
    session_id: str, user_id: str, status: str
) -> Optional[dict]:
    session = await get_session(session_id, user_id)
    if session is None:
        return None
    if session["status"] != "active":
        return session
    now = _now()
    if _mem() is not None:
        session["status"] = status
        session["ended_at"] = now
        _mem().sessions[str(session["id"])] = session
        return deepcopy(session)
    row = await db.pool().fetchrow(
        """
        UPDATE workout_sessions
        SET status = $3, ended_at = now()
        WHERE id = $1::uuid AND user_id = $2::uuid AND status = 'active'
        RETURNING id, user_id, session_date, plan_day_id, status, started_at, ended_at
        """,
        session_id,
        user_id,
        status,
    )
    if row:
        return dict(row)
    refreshed = await get_session(session_id, user_id)
    return refreshed


async def list_sessions(
    user_id: str,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
    before: Optional[datetime] = None,
) -> list[dict]:
    limit = clamp_limit(limit, DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT)
    if _mem() is not None:
        rows = [s for s in _mem().sessions.values() if s["user_id"] == user_id]
        if before is not None:
            rows = [s for s in rows if s["started_at"] < before]
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return deepcopy(rows[:limit])
    if before is None:
        rows = await db.pool().fetch(
            """
            SELECT id, user_id, session_date, plan_day_id, status, started_at, ended_at
            FROM workout_sessions
            WHERE user_id = $1::uuid
            ORDER BY started_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    else:
        rows = await db.pool().fetch(
            """
            SELECT id, user_id, session_date, plan_day_id, status, started_at, ended_at
            FROM workout_sessions
            WHERE user_id = $1::uuid AND started_at < $3
            ORDER BY started_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
            before,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Sets + PRs
# ---------------------------------------------------------------------------

async def list_set_logs(session_id: str, user_id: str) -> list[dict]:
    session_id = normalized_uuid(session_id)
    if session_id is None:
        return []
    session = await get_session(session_id, user_id)
    if session is None:
        return []
    if _mem() is not None:
        rows = [
            s
            for s in _mem().sets.values()
            if s["session_id"] == session_id and s["user_id"] == user_id
        ]
        rows.sort(key=lambda r: (r["completed_at"], r["set_number"]))
        return deepcopy(rows)
    rows = await db.pool().fetch(
        """
        SELECT id, user_id, session_id, exercise_id, set_number, reps, weight,
               completed_at, source
        FROM set_logs
        WHERE session_id = $1::uuid AND user_id = $2::uuid
        ORDER BY completed_at, set_number
        """,
        session_id,
        user_id,
    )
    return [dict(r) for r in rows]


async def insert_set_log(
    user_id: str,
    session_id: str,
    exercise_id: str,
    set_number: int,
    reps: Optional[int],
    weight: Optional[float],
    source: str = "voice",
) -> dict:
    session_id = normalized_uuid(session_id)
    exercise_id = normalized_uuid(exercise_id)
    if session_id is None or exercise_id is None:
        raise ValueError("session_id and exercise_id must be UUIDs")
    now = _now()
    row_id = str(uuid.uuid4())
    row = {
        "id": row_id,
        "user_id": user_id,
        "session_id": session_id,
        "exercise_id": exercise_id,
        "set_number": int(set_number),
        "reps": reps,
        "weight": weight,
        "completed_at": now,
        "source": source,
    }
    if _mem() is not None:
        for existing in _mem().sets.values():
            if (
                existing["session_id"] == session_id
                and existing["exercise_id"] == exercise_id
                and int(existing["set_number"]) == int(set_number)
            ):
                raise DuplicateSetError()
        _mem().sets[row_id] = row
        return deepcopy(row)
    try:
        inserted = await db.pool().fetchrow(
            """
            INSERT INTO set_logs (
                id, user_id, session_id, exercise_id, set_number, reps, weight, source
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7, $8)
            RETURNING id, user_id, session_id, exercise_id, set_number, reps, weight,
                      completed_at, source
            """,
            row_id,
            user_id,
            session_id,
            exercise_id,
            int(set_number),
            reps,
            weight,
            source,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateSetError() from exc
    return dict(inserted)


async def get_personal_record(
    user_id: str, exercise_id: str, rep_range: int
) -> Optional[dict]:
    exercise_id = normalized_uuid(exercise_id)
    if exercise_id is None:
        return None
    if _mem() is not None:
        row = _mem().prs.get((user_id, exercise_id, int(rep_range)))
        return deepcopy(row) if row else None
    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, exercise_id, rep_range, weight, achieved_at
        FROM personal_records
        WHERE user_id = $1::uuid AND exercise_id = $2::uuid AND rep_range = $3
        """,
        user_id,
        exercise_id,
        int(rep_range),
    )
    return dict(row) if row else None


async def upsert_personal_record(
    user_id: str, exercise_id: str, rep_range: int, weight: float
) -> tuple[dict, bool]:
    """Return (row, is_new_pr). Never overwrites a heavier existing best."""
    exercise_id = normalized_uuid(exercise_id)
    if exercise_id is None:
        raise ValueError("exercise_id must be a UUID")
    existing = await get_personal_record(user_id, exercise_id, rep_range)
    if existing is not None and float(weight) <= float(existing["weight"]):
        return existing, False
    now = _now()
    if _mem() is not None:
        key = (user_id, exercise_id, int(rep_range))
        if existing is None:
            row = {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "exercise_id": exercise_id,
                "rep_range": int(rep_range),
                "weight": float(weight),
                "achieved_at": now,
            }
        else:
            row = dict(existing)
            row["weight"] = float(weight)
            row["achieved_at"] = now
        _mem().prs[key] = row
        return deepcopy(row), True
    row = await db.pool().fetchrow(
        """
        INSERT INTO personal_records (user_id, exercise_id, rep_range, weight)
        VALUES ($1::uuid, $2::uuid, $3, $4)
        ON CONFLICT (user_id, exercise_id, rep_range) DO UPDATE
        SET weight = EXCLUDED.weight, achieved_at = now()
        WHERE EXCLUDED.weight > personal_records.weight
        RETURNING id, user_id, exercise_id, rep_range, weight, achieved_at
        """,
        user_id,
        exercise_id,
        int(rep_range),
        float(weight),
    )
    if row is None:
        current = await get_personal_record(user_id, exercise_id, rep_range)
        return current or {
            "user_id": user_id,
            "exercise_id": exercise_id,
            "rep_range": int(rep_range),
            "weight": float(weight),
        }, False
    return dict(row), True


async def list_personal_records(user_id: str, *, limit: int = DEFAULT_PR_LIMIT) -> list[dict]:
    limit = clamp_limit(limit, DEFAULT_PR_LIMIT, MAX_PR_LIMIT)
    if _mem() is not None:
        rows = [r for r in _mem().prs.values() if r["user_id"] == user_id]
        rows.sort(key=lambda r: r["achieved_at"], reverse=True)
        out = []
        for row in rows[:limit]:
            copied = deepcopy(row)
            ex = _mem().exercises.get(str(row["exercise_id"]))
            if ex and ex["user_id"] == user_id:
                copied["exercise_name"] = ex["name"]
            out.append(copied)
        return out
    rows = await db.pool().fetch(
        """
        SELECT pr.id, pr.user_id, pr.exercise_id, pr.rep_range, pr.weight, pr.achieved_at,
               pe.name AS exercise_name
        FROM personal_records pr
        JOIN planned_exercises pe
          ON pe.id = pr.exercise_id AND pe.user_id = pr.user_id
        WHERE pr.user_id = $1::uuid
        ORDER BY pr.achieved_at DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def list_exercise_history(
    user_id: str,
    exercise_id: str,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[dict]:
    exercise_id = normalized_uuid(exercise_id)
    if exercise_id is None:
        return []
    owned = await get_exercise(exercise_id, user_id)
    if owned is None:
        return []
    limit = clamp_limit(limit, DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT)
    if _mem() is not None:
        rows = [
            s
            for s in _mem().sets.values()
            if s["user_id"] == user_id and s["exercise_id"] == exercise_id
        ]
        rows.sort(key=lambda r: r["completed_at"], reverse=True)
        return deepcopy(rows[:limit])
    rows = await db.pool().fetch(
        """
        SELECT sl.id, sl.user_id, sl.session_id, sl.exercise_id, sl.set_number,
               sl.reps, sl.weight, sl.completed_at, sl.source
        FROM set_logs sl
        WHERE sl.user_id = $1::uuid AND sl.exercise_id = $2::uuid
        ORDER BY sl.completed_at DESC
        LIMIT $3
        """,
        user_id,
        exercise_id,
        limit,
    )
    return [dict(r) for r in rows]


async def adherence_counts(user_id: str, *, window_days: int = 28) -> dict:
    if _mem() is not None:
        sessions = [s for s in _mem().sessions.values() if s["user_id"] == user_id]
        sets = [s for s in _mem().sets.values() if s["user_id"] == user_id]
        completed = [s for s in sessions if s["status"] == "completed"]
        abandoned = [s for s in sessions if s["status"] == "abandoned"]
        cutoff = _now().date().toordinal() - window_days
        recent = [
            s
            for s in completed
            if s["started_at"].date().toordinal() >= cutoff
        ]
        last = None
        finished = [s for s in sessions if s["status"] in ("completed", "abandoned")]
        if finished:
            finished.sort(key=lambda r: r.get("ended_at") or r["started_at"], reverse=True)
            last = finished[0]
        return {
            "sessions_completed": len(completed),
            "sessions_abandoned": len(abandoned),
            "sets_completed": len(sets),
            "last_workout": last,
            "recent_completed_in_window": len(recent),
            "window_days": window_days,
        }
    row = await db.pool().fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'completed') AS sessions_completed,
            COUNT(*) FILTER (WHERE status = 'abandoned') AS sessions_abandoned
        FROM workout_sessions
        WHERE user_id = $1::uuid
        """,
        user_id,
    )
    sets_row = await db.pool().fetchval(
        "SELECT COUNT(*) FROM set_logs WHERE user_id = $1::uuid",
        user_id,
    )
    last = await db.pool().fetchrow(
        """
        SELECT id, user_id, session_date, plan_day_id, status, started_at, ended_at
        FROM workout_sessions
        WHERE user_id = $1::uuid AND status IN ('completed', 'abandoned')
        ORDER BY COALESCE(ended_at, started_at) DESC
        LIMIT 1
        """,
        user_id,
    )
    recent = await db.pool().fetchval(
        """
        SELECT COUNT(*) FROM workout_sessions
        WHERE user_id = $1::uuid
          AND status = 'completed'
          AND started_at >= (now() - ($2::int * INTERVAL '1 day'))
        """,
        user_id,
        int(window_days),
    )
    return {
        "sessions_completed": int(row["sessions_completed"] or 0) if row else 0,
        "sessions_abandoned": int(row["sessions_abandoned"] or 0) if row else 0,
        "sets_completed": int(sets_row or 0),
        "last_workout": dict(last) if last else None,
        "recent_completed_in_window": int(recent or 0),
        "window_days": int(window_days),
    }


async def list_sessions_on_day(user_id: str, day: date) -> list[dict]:
    if _mem() is not None:
        rows = [
            s
            for s in _mem().sessions.values()
            if s["user_id"] == user_id and s["started_at"].date() == day
        ]
        rows.sort(key=lambda r: r["started_at"])
        return deepcopy(rows)
    rows = await db.pool().fetch(
        """
        SELECT id, user_id, session_date, plan_day_id, status, started_at, ended_at
        FROM workout_sessions
        WHERE user_id = $1::uuid
          AND started_at >= ($2::date)::timestamp AT TIME ZONE 'UTC'
          AND started_at < ($2::date + 1)::timestamp AT TIME ZONE 'UTC'
        ORDER BY started_at ASC
        """,
        user_id,
        day,
    )
    return [dict(r) for r in rows]


async def list_recent_checkins(
    user_id: str,
    *,
    days: int = DEFAULT_CHECKIN_DAYS,
    limit: int = DEFAULT_CHECKIN_LIMIT,
) -> list[dict]:
    days = max(1, min(int(days), MAX_CHECKIN_DAYS))
    limit = clamp_limit(limit, DEFAULT_CHECKIN_LIMIT, MAX_CHECKIN_DAYS)
    if _mem() is not None:
        rows = [c for c in _mem().checkins if c["user_id"] == user_id]
        rows.sort(key=lambda r: r["local_date"], reverse=True)
        return deepcopy(rows[:limit])
    rows = await db.pool().fetch(
        """
        SELECT id, user_id, local_date, timezone, status, sleep_hours, sleep_quality,
               energy, mood, stress, soreness, top_priority, notes, summary,
               completed_at
        FROM daily_checkins
        WHERE user_id = $1::uuid
          AND local_date >= (CURRENT_DATE - $2::int)
        ORDER BY local_date DESC
        LIMIT $3
        """,
        user_id,
        days,
        limit,
    )
    return [dict(r) for r in rows]


def memory_seed_checkin(user_id: str, **fields: Any) -> dict:
    store = _mem()
    if store is None:
        raise RuntimeError("memory-store helper called while Postgres store is active")
    row = {"id": str(uuid.uuid4()), "user_id": user_id, **fields}
    store.checkins.append(row)
    return deepcopy(row)
