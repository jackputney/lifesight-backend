"""Bounded Fitness Claude tools. Never dump lifetime history into the prompt."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import HTTPException

from shared.fitness import service, store
from shared.health.tools import GET_RECENT_HEALTH_DATA_TOOL

_PLAN_TOOL = {
    "name": "get_current_workout_plan",
    "description": (
        "Read the user's active workout plan (days and planned exercises). "
        "Call when programming today's session or answering what the plan is. "
        "Does not include lifetime history."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}

_ACTIVE_TOOL = {
    "name": "get_active_workout",
    "description": (
        "Read the in-progress workout session: current exercise, current set, "
        "sets already logged. Call when the user asks where they are in the "
        "session or how many sets are left."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}

_RECENT_TOOL = {
    "name": "get_recent_workouts",
    "description": (
        "Read a bounded list of recent workout sessions (status, date). "
        "Default 10, max 20. Not lifetime history."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    },
}

_HISTORY_TOOL = {
    "name": "get_exercise_history",
    "description": (
        "Read recent logged sets for one planned exercise (bounded). "
        "Requires a real exercise_id from the current plan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "exercise_id": {"type": "string", "description": "planned_exercises UUID"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["exercise_id"],
    },
}

_PR_TOOL = {
    "name": "get_personal_records",
    "description": (
        "Read personal records stored BY REP RANGE. A 5-rep best is not a "
        "1-rep best. Weight is a unitless number."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    },
}

_CHECKIN_TOOL = {
    "name": "get_recent_checkins",
    "description": (
        "Read recent Daily Check-In rows (sleep, energy, mood, stress, soreness). "
        "This is self-reported check-in data, NOT HealthKit. Do not treat the "
        "two sources as the same, and do not claim causal links (e.g. that "
        "training caused poor sleep) when the rows only coexist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 14},
        },
    },
}

_START_TOOL = {
    "name": "start_workout",
    "description": (
        "Start today's workout or resume the existing active session. "
        "If a different day's session is already active, this returns an error "
        "instead of abandoning it. Ordinary start is not Confirm Gate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "plan_day_id": {
                "type": ["string", "null"],
                "description": "UUID of a workout_days row; omit to use the next plan day.",
            }
        },
    },
}

_LOG_TOOL = {
    "name": "log_workout_set",
    "description": (
        "Log one set on the active workout. Not Confirm Gate. "
        "Pass exercise_id when known; otherwise the current exercise is used. "
        "Do not invent weights or reps the user did not say."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "exercise_id": {"type": ["string", "null"]},
            "exercise_name": {"type": ["string", "null"]},
            "set_number": {"type": ["integer", "null"], "minimum": 1, "maximum": 30},
            "reps": {"type": ["integer", "null"], "minimum": 0, "maximum": 500},
            "weight": {"type": ["number", "null"], "minimum": 0, "maximum": 2000},
        },
    },
}

_COMPLETE_TOOL = {
    "name": "complete_workout",
    "description": "Mark the active workout completed. Not Confirm Gate.",
    "input_schema": {"type": "object", "properties": {}},
}

_ABANDON_TOOL = {
    "name": "abandon_workout",
    "description": (
        "Abandon the active workout without completing it. Use only when the "
        "user clearly wants to stop/cancel the session. Not Confirm Gate."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

FITNESS_DOMAIN_TOOLS: list[dict] = [
    _PLAN_TOOL,
    _ACTIVE_TOOL,
    _RECENT_TOOL,
    _HISTORY_TOOL,
    _PR_TOOL,
    GET_RECENT_HEALTH_DATA_TOOL,
    _CHECKIN_TOOL,
    _START_TOOL,
    _LOG_TOOL,
    _COMPLETE_TOOL,
    _ABANDON_TOOL,
]


def _dump(payload: Any) -> str:
    return json.dumps(payload, default=str, separators=(",", ":"))


def _tool_error(exc: HTTPException) -> str:
    return f"Error: {exc.detail}"


async def run_fitness_tool(name: str, user_id: str, tool_input: dict[str, Any]) -> str:
    data = tool_input if isinstance(tool_input, dict) else {}
    try:
        if name == "get_current_workout_plan":
            plan = await service.get_current_plan(user_id)
            if plan is None:
                return "No active workout plan. The user has not saved a plan yet."
            return _dump(plan)

        if name == "get_active_workout":
            active = await service.get_active_workout(user_id)
            if active is None:
                return "No active workout session."
            return _dump(active)

        if name == "get_recent_workouts":
            history = await service.list_history(user_id, limit=data.get("limit") or 10)
            return _dump(history)

        if name == "get_exercise_history":
            return _dump(
                await service.exercise_history(
                    str(data.get("exercise_id") or ""),
                    user_id,
                    limit=data.get("limit"),
                )
            )

        if name == "get_personal_records":
            return _dump(await service.list_prs(user_id, limit=data.get("limit")))

        if name == "get_recent_checkins":
            rows = await store.list_recent_checkins(
                user_id, days=int(data.get("days") or store.DEFAULT_CHECKIN_DAYS)
            )
            compact = []
            for row in rows:
                compact.append(
                    {
                        "local_date": str(row.get("local_date")),
                        "status": row.get("status"),
                        "sleep_hours": row.get("sleep_hours"),
                        "energy": row.get("energy"),
                        "mood": row.get("mood"),
                        "stress": row.get("stress"),
                        "soreness": row.get("soreness"),
                        "source": "daily_checkin",
                    }
                )
            return _dump(
                {
                    "source": "daily_checkin",
                    "note": (
                        "Self-reported Daily Check-In values. Distinct from "
                        "HealthKit/wearable samples. Do not infer causation."
                    ),
                    "checkins": compact,
                }
            )

        if name == "start_workout":
            raw_day = data.get("plan_day_id")
            if raw_day in ("", "null", "none"):
                raw_day = None
            state = await service.start_workout(user_id, raw_day)
            return _dump(state)

        if name == "log_workout_set":
            active = await store.get_active_session(user_id)
            if active is None:
                return "Error: no active workout. Start a workout first."
            result = await service.log_set(
                user_id,
                str(active["id"]),
                exercise_id=data.get("exercise_id"),
                exercise_name=data.get("exercise_name"),
                set_number=data.get("set_number"),
                reps=data.get("reps"),
                weight=data.get("weight"),
                source="voice",
            )
            return _dump(
                {
                    "set": result["set"],
                    "pr_announcement": result["pr_announcement"],
                    "state": result["state"],
                }
            )

        if name == "complete_workout":
            active = await store.get_active_session(user_id)
            if active is None:
                return "Error: no active workout."
            return _dump(await service.complete_workout(str(active["id"]), user_id))

        if name == "abandon_workout":
            active = await store.get_active_session(user_id)
            if active is None:
                return "Error: no active workout."
            return _dump(await service.abandon_workout(str(active["id"]), user_id))
    except HTTPException as exc:
        return _tool_error(exc)
    return f"Error: unknown fitness tool '{name}'."
