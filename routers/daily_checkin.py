"""Daily check-in status + start (Home card). Not Confirm Gate."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from shared.auth import get_current_user_id
from shared.daily_checkin import DailyCheckinOut, get_today_checkin, start_today_checkin

router = APIRouter(prefix="/daily-checkin", tags=["daily-checkin"])


@router.get("/today", response_model=DailyCheckinOut)
async def read_today_checkin(user_id: str = Depends(get_current_user_id)):
    """Today's check-in for the user's profile timezone (falls back to UTC)."""
    return await get_today_checkin(user_id)


@router.post("/start", response_model=DailyCheckinOut)
async def start_checkin(user_id: str = Depends(get_current_user_id)):
    """Idempotent start/resume for today's local date.

    in_progress → resume; completed → return completed; else create.
    """
    return await start_today_checkin(user_id)
