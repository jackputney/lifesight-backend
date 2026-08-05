"""Self-hosted username/password auth routes."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from shared.auth import get_current_session_id, get_current_user_id
from shared.local_auth.service import AuthError, AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=8)
    email: Optional[str] = None
    display_name: Optional[str] = None
    device_name: Optional[str] = None


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    device_name: Optional[str] = None


class RefreshIn(BaseModel):
    refresh_token: str = Field(..., min_length=10)


class LogoutIn(BaseModel):
    refresh_token: Optional[str] = None


class PatchMeIn(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    clear_email: bool = False


class UserOut(BaseModel):
    id: str
    username: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    is_active: bool = True
    created_at: Any = None
    updated_at: Any = None


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "bearer"
    user: UserOut


class MessageOut(BaseModel):
    detail: str


class ProtectedOut(BaseModel):
    ok: bool
    user_id: str


def _raise(exc: AuthError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _client_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{username.strip().lower()}"


@router.post("/register", response_model=TokenOut)
async def register(body: RegisterIn):
    try:
        return await AuthService().register(
            username=body.username,
            password=body.password,
            email=body.email,
            display_name=body.display_name,
            device_name=body.device_name,
        )
    except AuthError as exc:
        _raise(exc)


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request):
    try:
        return await AuthService().login(
            username=body.username,
            password=body.password,
            device_name=body.device_name,
            rate_key=_client_key(request, body.username),
        )
    except AuthError as exc:
        _raise(exc)


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn):
    try:
        return await AuthService().refresh(body.refresh_token)
    except AuthError as exc:
        _raise(exc)


@router.post("/logout", response_model=MessageOut)
async def logout(
    body: LogoutIn,
    user_id: str = Depends(get_current_user_id),
    session_id: str = Depends(get_current_session_id),
):
    _ = user_id
    try:
        await AuthService().logout(
            refresh_token=body.refresh_token,
            session_id=None if body.refresh_token else session_id,
        )
    except AuthError as exc:
        _raise(exc)
    return MessageOut(detail="Logged out")


@router.post("/logout-all", response_model=MessageOut)
async def logout_all(user_id: str = Depends(get_current_user_id)):
    try:
        n = await AuthService().logout_all(user_id)
    except AuthError as exc:
        _raise(exc)
    return MessageOut(detail=f"Revoked {n} session(s)")


@router.get("/me", response_model=UserOut)
async def me(user_id: str = Depends(get_current_user_id)):
    try:
        return await AuthService().get_me(user_id)
    except AuthError as exc:
        _raise(exc)


@router.patch("/me", response_model=UserOut)
async def patch_me(body: PatchMeIn, user_id: str = Depends(get_current_user_id)):
    try:
        return await AuthService().patch_me(
            user_id,
            display_name=body.display_name,
            email=body.email,
            clear_email=body.clear_email,
        )
    except AuthError as exc:
        _raise(exc)


@router.get("/protected", response_model=ProtectedOut)
async def protected(user_id: str = Depends(get_current_user_id)):
    """Smoke route proving Depends(get_current_user_id) gates access."""
    return ProtectedOut(ok=True, user_id=user_id)
