"""Single workout-progress engine — shared by HTTP state, set logging, and visual_panel."""

from __future__ import annotations

from typing import Any, Optional

from shared.fitness import store


def progress_from_logs(
    exercises: list[dict],
    logs: list[dict],
) -> dict[str, Any]:
    """Compute current exercise / set from planned exercises + logged sets.

    This is the only progression algorithm. HTTP state, chat visual_panel, and
    set-number assignment all call it so they cannot drift.
    """
    logs_by_ex: dict[str, list[dict]] = {}
    for lg in logs:
        logs_by_ex.setdefault(str(lg["exercise_id"]), []).append(lg)

    current_exercise = None
    current_set_number = 1
    if exercises:
        for ex in exercises:
            eid = str(ex["id"])
            done = len(logs_by_ex.get(eid, []))
            target = int(ex["target_sets"] or 0)
            if target == 0:
                if done == 0:
                    current_exercise = ex
                    current_set_number = 1
                    break
                continue
            if done < target:
                current_exercise = ex
                current_set_number = done + 1
                break
        if current_exercise is None:
            current_exercise = exercises[-1]
            current_set_number = len(logs_by_ex.get(str(current_exercise["id"]), [])) + 1

    remaining_sets = 0
    if current_exercise is not None:
        eid = str(current_exercise["id"])
        done = len(logs_by_ex.get(eid, []))
        target = int(current_exercise.get("target_sets") or 0)
        remaining_sets = max(target - done, 0)

    return {
        "exercises": exercises,
        "logs": logs,
        "logs_by_exercise": logs_by_ex,
        "current_exercise": current_exercise,
        "current_set_number": current_set_number,
        "remaining_sets_on_current": remaining_sets,
        "sets_logged": len(logs),
    }


async def session_progress(session: dict, user_id: str) -> dict[str, Any]:
    exercises: list[dict] = []
    if session.get("plan_day_id"):
        exercises = await store.list_exercises_for_day(str(session["plan_day_id"]), user_id)
    logs = await store.list_set_logs(str(session["id"]), user_id)
    payload = progress_from_logs(exercises, logs)
    payload["session"] = session
    return payload


def next_set_number(progress: dict[str, Any], exercise_id: str) -> int:
    logs = progress["logs_by_exercise"].get(str(exercise_id), [])
    if not logs:
        return 1
    return max(int(lg["set_number"]) for lg in logs) + 1


def exercise_in_session(progress: dict[str, Any], exercise_id: str) -> Optional[dict]:
    for ex in progress["exercises"]:
        if str(ex["id"]) == str(exercise_id):
            return ex
    return None
