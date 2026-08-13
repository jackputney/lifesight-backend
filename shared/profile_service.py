"""Profile read/patch helpers shared by HTTP and seed script."""

from __future__ import annotations

import json
from typing import Any, Optional

from shared import db
from shared.profile_schema import ProfileOut, ProfilePatch, empty_profile


def row_to_profile(row: dict, *, display_name: Optional[str], user_id: str) -> ProfileOut:
    def _list(key: str) -> list[str]:
        raw = row.get(key)
        if raw is None:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return []
        if isinstance(raw, list):
            return [str(x) for x in raw if str(x).strip()]
        return []

    height = row.get("height_cm")
    weight = row.get("weight_kg")
    return ProfileOut(
        user_id=user_id,
        display_name=display_name,
        timezone=row.get("timezone"),
        date_of_birth=row.get("date_of_birth"),
        height_cm=float(height) if height is not None else None,
        weight_kg=float(weight) if weight is not None else None,
        interaction_style=row.get("interaction_style"),
        vision_preference=row.get("vision_preference"),
        spoken_response_preference=row.get("spoken_response_preference"),
        experience_level=row.get("experience_level"),
        primary_goals=_list("primary_goals"),
        training_frequency=row.get("training_frequency"),
        available_equipment=_list("available_equipment"),
        injuries_limitations=row.get("injuries_limitations"),
        nutrition_goal=row.get("nutrition_goal"),
        dietary_preferences=_list("dietary_preferences"),
        allergies_restrictions=_list("allergies_restrictions"),
        preferred_units=row.get("preferred_units"),
        training_environment=row.get("training_environment"),
        typical_session_minutes=(
            int(row["typical_session_minutes"])
            if row.get("typical_session_minutes") is not None
            else None
        ),
        sex_for_physiological_calculations=row.get(
            "sex_for_physiological_calculations"
        ),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


async def get_profile(user_id: str) -> ProfileOut:
    display_name = await db.get_user_display_name(user_id)
    row = await db.get_user_profile_row(user_id)
    if row is None:
        return empty_profile(user_id, display_name=display_name)
    return row_to_profile(row, display_name=display_name, user_id=user_id)


async def patch_profile(user_id: str, patch: ProfilePatch) -> ProfileOut:
    updates = patch.as_update_dict()
    if updates:
        await db.upsert_user_profile(user_id, updates)
    return await get_profile(user_id)
