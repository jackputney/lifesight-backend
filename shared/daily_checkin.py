"""Daily check-in V1 — dated recovery/mood state, not permanent profile."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from shared import db
from shared.profile_service import get_profile

CheckinStatus = Literal["not_started", "in_progress", "completed"]


class DailyCheckinOut(BaseModel):
    id: Optional[str] = None
    user_id: str
    local_date: str
    timezone: str
    conversation_id: Optional[str] = None
    status: CheckinStatus
    sleep_hours: Optional[float] = None
    sleep_quality: Optional[int] = None
    energy: Optional[int] = None
    mood: Optional[int] = None
    stress: Optional[int] = None
    soreness: Optional[int] = None
    top_priority: Optional[str] = None
    notes: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DailyCheckinPatch(BaseModel):
    sleep_hours: Optional[float] = Field(default=None, ge=0, le=24)
    sleep_quality: Optional[int] = Field(default=None, ge=1, le=5)
    energy: Optional[int] = Field(default=None, ge=1, le=5)
    mood: Optional[int] = Field(default=None, ge=1, le=5)
    stress: Optional[int] = Field(default=None, ge=1, le=5)
    soreness: Optional[int] = Field(default=None, ge=1, le=5)
    top_priority: Optional[str] = Field(default=None, max_length=240)
    notes: Optional[str] = Field(default=None, max_length=2000)
    summary: Optional[str] = Field(default=None, max_length=2000)
    mark_completed: bool = False

    @field_validator("top_priority", "notes", "summary")
    @classmethod
    def _trim(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        return text or None


COMPLETE_DAILY_CHECKIN_TOOL: dict = {
    "name": "update_daily_checkin",
    "description": (
        "Update today's structured daily check-in from explicit user answers. "
        "Pass only fields the user actually answered. Set mark_completed=true "
        "when the check-in is finished and include a concise useful summary. "
        "Do not invent medical diagnoses."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sleep_hours": {"type": ["number", "null"], "minimum": 0, "maximum": 24},
            "sleep_quality": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
            "energy": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
            "mood": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
            "stress": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
            "soreness": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
            "top_priority": {"type": ["string", "null"], "maxLength": 240},
            "notes": {"type": ["string", "null"], "maxLength": 2000},
            "summary": {"type": ["string", "null"], "maxLength": 2000},
            "mark_completed": {"type": "boolean"},
        },
        "required": [],
    },
}


def resolve_local_date(*, timezone_name: Optional[str], now: Optional[datetime] = None) -> tuple[date, str]:
    """Return (local_date, timezone_used). Falls back to UTC if tz invalid."""
    tz_name = (timezone_name or "").strip() or "UTC"
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz_name = "UTC"
        zone = ZoneInfo("UTC")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(zone)
    return local.date(), tz_name


def row_to_checkin(row: dict, *, user_id: str) -> DailyCheckinOut:
    return DailyCheckinOut(
        id=str(row["id"]) if row.get("id") is not None else None,
        user_id=user_id,
        local_date=str(row["local_date"]),
        timezone=row.get("timezone") or "UTC",
        conversation_id=(
            str(row["conversation_id"]) if row.get("conversation_id") else None
        ),
        status=row.get("status") or "not_started",
        sleep_hours=row.get("sleep_hours"),
        sleep_quality=row.get("sleep_quality"),
        energy=row.get("energy"),
        mood=row.get("mood"),
        stress=row.get("stress"),
        soreness=row.get("soreness"),
        top_priority=row.get("top_priority"),
        notes=row.get("notes"),
        summary=row.get("summary"),
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        updated_at=row.get("updated_at"),
    )


def empty_checkin(*, user_id: str, local_date: date, timezone_name: str) -> DailyCheckinOut:
    return DailyCheckinOut(
        user_id=user_id,
        local_date=str(local_date),
        timezone=timezone_name,
        status="not_started",
    )


async def get_today_checkin(user_id: str) -> DailyCheckinOut:
    profile = await get_profile(user_id)
    local_date, tz_name = resolve_local_date(timezone_name=profile.timezone)
    row = await db.get_daily_checkin(user_id, local_date)
    if row is None:
        return empty_checkin(user_id=user_id, local_date=local_date, timezone_name=tz_name)
    return row_to_checkin(row, user_id=user_id)


async def _ensure_checkin_conversation(
    user_id: str, *, local_date: date, existing_conversation_id: Optional[str]
) -> str:
    if existing_conversation_id:
        return str(existing_conversation_id)
    conversation_id = str(uuid.uuid4())
    title = f"Daily check-in {local_date.isoformat()}"
    await db.create_conversation(conversation_id, user_id, "checkin", title=title)
    return conversation_id


async def start_today_checkin(user_id: str) -> DailyCheckinOut:
    """Idempotent start/resume for today's local date."""
    profile = await get_profile(user_id)
    local_date, tz_name = resolve_local_date(timezone_name=profile.timezone)
    existing = await db.get_daily_checkin(user_id, local_date)
    if existing is not None:
        status = existing.get("status")
        if status == "completed":
            return row_to_checkin(existing, user_id=user_id)
        if status == "in_progress" and existing.get("conversation_id"):
            return row_to_checkin(existing, user_id=user_id)
        conversation_id = await _ensure_checkin_conversation(
            user_id,
            local_date=local_date,
            existing_conversation_id=(
                str(existing["conversation_id"])
                if existing.get("conversation_id")
                else None
            ),
        )
        row = await db.upsert_daily_checkin_start(
            user_id=user_id,
            local_date=local_date,
            timezone_name=tz_name,
            conversation_id=conversation_id,
        )
        return row_to_checkin(row, user_id=user_id)

    conversation_id = await _ensure_checkin_conversation(
        user_id, local_date=local_date, existing_conversation_id=None
    )
    row = await db.upsert_daily_checkin_start(
        user_id=user_id,
        local_date=local_date,
        timezone_name=tz_name,
        conversation_id=conversation_id,
    )
    return row_to_checkin(row, user_id=user_id)


async def apply_checkin_tool_update(
    user_id: str,
    tool_input: dict,
    *,
    conversation_id: str,
) -> str:
    if not isinstance(tool_input, dict):
        return "Error: invalid tool input."
    try:
        patch = DailyCheckinPatch.model_validate(
            {k: v for k, v in tool_input.items() if k in DailyCheckinPatch.model_fields}
        )
    except Exception as exc:
        return f"Error: invalid check-in values ({exc})."

    profile = await get_profile(user_id)
    local_date, tz_name = resolve_local_date(timezone_name=profile.timezone)
    updates = patch.model_dump(exclude_unset=True, exclude={"mark_completed"})
    row = await db.update_daily_checkin_fields(
        user_id=user_id,
        local_date=local_date,
        timezone_name=tz_name,
        conversation_id=conversation_id,
        fields=updates,
        mark_completed=bool(patch.mark_completed),
    )
    if row is None:
        return "Error: could not update today's check-in."
    if patch.mark_completed:
        return (
            "Daily check-in marked completed. Give a short spoken wrap-up and "
            "invite the user back to Home."
        )
    return "Daily check-in fields updated."


def compact_checkin_for_context(checkin: Optional[DailyCheckinOut]) -> str:
    """Bounded today's/latest check-in block for Fitness/Diet prompts."""
    if checkin is None or checkin.status == "not_started":
        return ""
    lines = [
        f"Today's daily check-in ({checkin.local_date}, {checkin.status}):"
    ]
    mapping = [
        ("sleep_hours", checkin.sleep_hours),
        ("sleep_quality", checkin.sleep_quality),
        ("energy", checkin.energy),
        ("mood", checkin.mood),
        ("stress", checkin.stress),
        ("soreness", checkin.soreness),
        ("top_priority", checkin.top_priority),
        ("summary", checkin.summary),
    ]
    for key, value in mapping:
        if value is not None and value != "":
            lines.append(f"- {key}: {value}")
    if len(lines) == 1:
        if checkin.status == "in_progress":
            lines.append("- (started; no structured answers yet)")
        else:
            return ""
    return "\n".join(lines)
