"""GET/PATCH /profile — domain LifeSight profile (not /auth/me)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from shared.auth import get_current_user_id
from shared.profile_schema import ProfileOut, ProfilePatch
from shared.profile_service import get_profile, patch_profile

router = APIRouter(tags=["profile"])


@router.get("/profile", response_model=ProfileOut)
async def read_profile(user_id: str = Depends(get_current_user_id)):
    return await get_profile(user_id)


@router.patch("/profile", response_model=ProfileOut)
async def update_profile(
    body: ProfilePatch,
    user_id: str = Depends(get_current_user_id),
):
    return await patch_profile(user_id, body)
