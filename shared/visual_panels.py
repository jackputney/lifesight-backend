"""Typed visual_panel payloads for /chat (V1: exercise).

Unknown panel types remain wire-compatible as {type, data} for older clients.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ExercisePanelData(BaseModel):
    """Structured Fitness exercise panel — no invented exercise_id."""

    exercise_id: Optional[str] = None
    exercise_name: str = Field(..., min_length=1, max_length=120)
    sets: int = Field(..., ge=1, le=100)
    reps: int = Field(..., ge=1, le=500)
    rest_seconds: int = Field(..., ge=0, le=3600)
    current_set: Optional[int] = Field(default=None, ge=1, le=100)
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("exercise_id", mode="before")
    @classmethod
    def _normalize_exercise_id(cls, value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return str(UUID(text))
        except ValueError as exc:
            raise ValueError("exercise_id must be a UUID when provided") from exc

    @field_validator("exercise_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        name = (value or "").strip()
        if not name:
            raise ValueError("exercise_name must be non-empty")
        return name

    @field_validator("notes", mode="before")
    @classmethod
    def _normalize_notes(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class VisualPanel(BaseModel):
    """Optional inline visual for chat UI. Additive — null stays valid."""

    type: str = Field(..., min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)


def exercise_visual_panel(data: ExercisePanelData) -> VisualPanel:
    return VisualPanel(type="exercise", data=data.model_dump(mode="json"))


def parse_exercise_panel_tool_input(tool_input: dict[str, Any]) -> ExercisePanelData:
    """Validate Claude tool input for present_exercise_panel.

    Omits exercise_id when unresolved — never invents one.
    """
    payload = dict(tool_input or {})
    if payload.get("exercise_id") in ("", "null", "none", "unknown"):
        payload["exercise_id"] = None
    return ExercisePanelData.model_validate(payload)
