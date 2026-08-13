"""Validated LifeSight user_profiles V1 — no untyped JSON bag.

Onboarding reuses these fields; iOS reads GET /profile to decide which
questions remain relevant. Backend profile data is authoritative — there is
no separate onboarding_complete flag.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

InteractionStyle = Literal["standard", "voice_first", "high_accessibility"]
PreferredUnits = Literal["imperial", "metric"]
TrainingEnvironment = Literal[
    "commercial_gym",
    "home_gym",
    "limited_equipment",
    "bodyweight_outdoors",
    "mixed",
]
# Canonical weekly frequency wire values (reuse training_frequency; no duplicate column).
TrainingFrequency = Literal["0_1", "2", "3", "4", "5", "6_plus"]
SexForPhysiologicalCalculations = Literal["male", "female", "unspecified"]

# Onboarding-controlled primary_goals values. Index 0 = primary; 1–2 = secondary.
# Legacy stored goals outside this set still decode on GET.
PRIMARY_GOAL_VALUES: frozenset[str] = frozenset(
    {
        "build_muscle",
        "get_stronger",
        "lose_body_fat",
        "improve_endurance",
        "general_fitness",
        "longevity_health",
        "track_nutrition",
        "return_to_training",
        "better_habits",
    }
)
TRAINING_FREQUENCY_VALUES: frozenset[str] = frozenset(
    {"0_1", "2", "3", "4", "5", "6_plus"}
)

MAX_ARRAY_ITEMS = 20
MAX_PRIMARY_GOALS = 3
MAX_SHORT_TEXT = 120
MAX_LONG_TEXT = 1000
MIN_SESSION_MINUTES = 10
MAX_SESSION_MINUTES = 300


def _normalize_str_list(
    value: Any,
    *,
    field: str,
    max_items: int = MAX_ARRAY_ITEMS,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array of strings")
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        if len(text) > MAX_SHORT_TEXT:
            raise ValueError(f"{field} items must be <= {MAX_SHORT_TEXT} characters")
        out.append(text)
        if len(out) > max_items:
            raise ValueError(f"{field} may have at most {max_items} items")
    return out


class ProfilePatch(BaseModel):
    """Partial update for PATCH /profile and seed script."""

    timezone: Optional[str] = Field(default=None, max_length=64)
    date_of_birth: Optional[date] = None
    height_cm: Optional[float] = Field(default=None, ge=30, le=300)
    weight_kg: Optional[float] = Field(default=None, ge=20, le=500)
    interaction_style: Optional[InteractionStyle] = None
    vision_preference: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT)
    spoken_response_preference: Optional[str] = Field(
        default=None, max_length=MAX_SHORT_TEXT
    )
    experience_level: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT)
    primary_goals: Optional[list[str]] = None
    training_frequency: Optional[TrainingFrequency] = None
    available_equipment: Optional[list[str]] = None
    injuries_limitations: Optional[str] = Field(default=None, max_length=MAX_LONG_TEXT)
    nutrition_goal: Optional[str] = Field(default=None, max_length=MAX_SHORT_TEXT)
    dietary_preferences: Optional[list[str]] = None
    allergies_restrictions: Optional[list[str]] = None
    preferred_units: Optional[PreferredUnits] = None
    training_environment: Optional[TrainingEnvironment] = None
    typical_session_minutes: Optional[int] = Field(
        default=None, ge=MIN_SESSION_MINUTES, le=MAX_SESSION_MINUTES
    )
    sex_for_physiological_calculations: Optional[SexForPhysiologicalCalculations] = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("primary_goals")
    @classmethod
    def _goals(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        goals = _normalize_str_list(
            value, field="primary_goals", max_items=MAX_PRIMARY_GOALS
        )
        unknown = [g for g in goals if g not in PRIMARY_GOAL_VALUES]
        if unknown:
            raise ValueError(
                "primary_goals must use supported onboarding values: "
                + ", ".join(sorted(PRIMARY_GOAL_VALUES))
                + f" (unsupported: {', '.join(unknown)})"
            )
        return goals

    @field_validator("available_equipment")
    @classmethod
    def _equipment(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        return _normalize_str_list(value, field="available_equipment")

    @field_validator("dietary_preferences")
    @classmethod
    def _diet(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        return _normalize_str_list(value, field="dietary_preferences")

    @field_validator("allergies_restrictions")
    @classmethod
    def _allergies(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        return _normalize_str_list(value, field="allergies_restrictions")

    def as_update_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class ProfileOut(BaseModel):
    """GET /profile response. display_name is convenience from users only.

    primary_goals is ordered: [0]=primary, [1–2]=optional secondary (max 3 in V1).
    training_frequency may contain legacy free-text from older rows; new writes
    use the canonical V1 literals documented in PRIMARY_GOAL / frequency constants.
    """

    user_id: str
    display_name: Optional[str] = None
    timezone: Optional[str] = None
    date_of_birth: Optional[date] = None
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    interaction_style: Optional[InteractionStyle] = None
    vision_preference: Optional[str] = None
    spoken_response_preference: Optional[str] = None
    experience_level: Optional[str] = None
    primary_goals: list[str] = Field(default_factory=list)
    training_frequency: Optional[str] = None
    available_equipment: list[str] = Field(default_factory=list)
    injuries_limitations: Optional[str] = None
    nutrition_goal: Optional[str] = None
    dietary_preferences: list[str] = Field(default_factory=list)
    allergies_restrictions: list[str] = Field(default_factory=list)
    preferred_units: Optional[PreferredUnits] = None
    training_environment: Optional[TrainingEnvironment] = None
    typical_session_minutes: Optional[int] = None
    sex_for_physiological_calculations: Optional[SexForPhysiologicalCalculations] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def empty_profile(user_id: str, *, display_name: Optional[str] = None) -> ProfileOut:
    """Safe defaults when no user_profiles row exists yet."""
    return ProfileOut(user_id=user_id, display_name=display_name)


def compact_profile_for_context(profile: ProfileOut) -> str:
    """Short non-secret profile block for Claude system context.

    Omits null/empty fields. Sex-for-calculations is included only when male
    or female (not as an identity label; unspecified/null omitted).
    """
    lines: list[str] = ["User profile (nullable fields omitted when unknown):"]
    mapping = [
        ("timezone", profile.timezone),
        ("preferred_units", profile.preferred_units),
        ("date_of_birth", str(profile.date_of_birth) if profile.date_of_birth else None),
        ("height_cm", profile.height_cm),
        ("weight_kg", profile.weight_kg),
        ("interaction_style", profile.interaction_style),
        ("vision_preference", profile.vision_preference),
        ("spoken_response_preference", profile.spoken_response_preference),
        ("experience_level", profile.experience_level),
        ("training_frequency", profile.training_frequency),
        ("training_environment", profile.training_environment),
        ("typical_session_minutes", profile.typical_session_minutes),
        ("nutrition_goal", profile.nutrition_goal),
        ("injuries_limitations", profile.injuries_limitations),
    ]
    for key, value in mapping:
        if value is not None and value != "":
            lines.append(f"- {key}: {value}")
    if profile.primary_goals:
        # Preserve order: first is primary.
        lines.append(f"- primary_goals (ordered): {', '.join(profile.primary_goals)}")
    if profile.available_equipment:
        lines.append(f"- available_equipment: {', '.join(profile.available_equipment)}")
    if profile.dietary_preferences:
        lines.append(f"- dietary_preferences: {', '.join(profile.dietary_preferences)}")
    if profile.allergies_restrictions:
        lines.append(
            f"- allergies_restrictions: {', '.join(profile.allergies_restrictions)}"
        )
    sex = profile.sex_for_physiological_calculations
    if sex in ("male", "female"):
        lines.append(
            "- sex_for_physiological_calculations: "
            f"{sex} (formula/reference use only; not gender identity)"
        )
    if len(lines) == 1:
        lines.append("- (no profile details on file)")
    return "\n".join(lines)
