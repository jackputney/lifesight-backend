"""Self-hosted auth business logic."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from shared.local_auth import passwords, tokens
from shared.local_auth.rate_limit import LOGIN_RATE_LIMITER, LoginRateLimiter
from shared.local_auth.store import (
    AuthStore,
    EmailTakenError,
    UsernameTakenError,
    get_store,
)

_USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")
GENERIC_INVALID = "Invalid credentials"


class AuthError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def normalize_email(email: Optional[str]) -> Optional[str]:
    if email is None:
        return None
    value = email.strip().lower()
    return value or None


def validate_username(username: str) -> str:
    normalized = normalize_username(username)
    if not _USERNAME_RE.match(normalized):
        raise AuthError(
            "Username must be 3–32 characters: lowercase letters, digits, underscore"
        )
    return normalized


def validate_password(password: str) -> str:
    if len(password or "") < 8:
        raise AuthError("Password must be at least 8 characters")
    return password


def validate_email(email: Optional[str]) -> Optional[str]:
    normalized = normalize_email(email)
    if normalized is None:
        return None
    if "@" not in normalized or "." not in normalized.split("@")[-1]:
        raise AuthError("Email is invalid")
    return normalized


class AuthService:
    def __init__(
        self,
        store: AuthStore | None = None,
        rate_limiter: LoginRateLimiter | None = None,
    ) -> None:
        self.store = store or get_store()
        self.rate_limiter = rate_limiter or LOGIN_RATE_LIMITER

    def _public_user(self, user: dict) -> dict[str, Any]:
        return {
            "id": str(user["id"]),
            "username": user["username"],
            "email": user.get("email"),
            "display_name": user.get("display_name"),
            "is_active": bool(user.get("is_active", True)),
            "created_at": user.get("created_at"),
            "updated_at": user.get("updated_at"),
        }

    async def register(
        self,
        *,
        username: str,
        password: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        device_name: Optional[str] = None,
    ) -> dict:
        username_n = validate_username(username)
        password_v = validate_password(password)
        email_n = validate_email(email)
        display = (display_name or "").strip() or None
        try:
            user = await self.store.create_user(
                username=username_n,
                email=email_n,
                password_hash=passwords.hash_password(password_v),
                display_name=display,
            )
        except UsernameTakenError as exc:
            raise AuthError("Username is already taken", status_code=409) from exc
        except EmailTakenError as exc:
            raise AuthError("Email is already taken", status_code=409) from exc
        return await self._issue_for_user(user, device_name=device_name)

    async def login(
        self,
        *,
        username: str,
        password: str,
        device_name: Optional[str] = None,
        rate_key: str | None = None,
    ) -> dict:
        username_n = normalize_username(username)
        key = rate_key or username_n
        if self.rate_limiter.is_blocked(key):
            raise AuthError("Too many login attempts. Try again later.", status_code=429)

        user = await self.store.get_user_by_username(username_n)
        ok = bool(
            user
            and user.get("is_active")
            and passwords.verify_password(user["password_hash"], password or "")
        )
        if not ok:
            self.rate_limiter.record_failure(key)
            raise AuthError(GENERIC_INVALID, status_code=401)

        self.rate_limiter.reset(key)
        if passwords.needs_rehash(user["password_hash"]):
            await self.store.update_user(
                str(user["id"]),
                password_hash=passwords.hash_password(password),
            )
        return await self._issue_for_user(user, device_name=device_name)

    async def _issue_for_user(
        self, user: dict, *, device_name: Optional[str]
    ) -> dict:
        raw_refresh = tokens.new_refresh_token()
        expires_at = tokens.refresh_expiry()
        session = await self.store.create_session(
            user_id=str(user["id"]),
            refresh_token_hash=tokens.hash_refresh_token(raw_refresh),
            expires_at=expires_at,
            device_name=device_name,
        )
        access, expires_in = tokens.mint_access_token(
            user_id=str(user["id"]),
            session_id=str(session["id"]),
        )
        return {
            "access_token": access,
            "refresh_token": raw_refresh,
            "expires_in": expires_in,
            "token_type": "bearer",
            "user": self._public_user(user),
        }

    async def refresh(self, refresh_token: str) -> dict:
        if not refresh_token:
            raise AuthError(GENERIC_INVALID, status_code=401)
        token_hash = tokens.hash_refresh_token(refresh_token)
        session = await self.store.get_session_by_refresh_hash(token_hash)
        now = datetime.now(timezone.utc)
        if (
            session is None
            or session.get("revoked_at") is not None
            or session["expires_at"] <= now
        ):
            raise AuthError(GENERIC_INVALID, status_code=401)

        user = await self.store.get_user_by_id(str(session["user_id"]))
        if not user or not user.get("is_active"):
            raise AuthError(GENERIC_INVALID, status_code=401)

        new_raw = tokens.new_refresh_token()
        rotated = await self.store.rotate_session_refresh(
            str(session["id"]),
            new_refresh_token_hash=tokens.hash_refresh_token(new_raw),
            expires_at=tokens.refresh_expiry(now),
            now=now,
        )
        if rotated is None:
            raise AuthError(GENERIC_INVALID, status_code=401)

        access, expires_in = tokens.mint_access_token(
            user_id=str(user["id"]),
            session_id=str(session["id"]),
            now=now,
        )
        return {
            "access_token": access,
            "refresh_token": new_raw,
            "expires_in": expires_in,
            "token_type": "bearer",
            "user": self._public_user(user),
        }

    async def logout(self, *, refresh_token: str | None = None, session_id: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        if refresh_token:
            session = await self.store.get_session_by_refresh_hash(
                tokens.hash_refresh_token(refresh_token)
            )
            if session:
                await self.store.revoke_session(str(session["id"]), now=now)
            return
        if session_id:
            await self.store.revoke_session(session_id, now=now)

    async def logout_all(self, user_id: str) -> int:
        return await self.store.revoke_all_sessions(
            user_id, now=datetime.now(timezone.utc)
        )

    async def get_me(self, user_id: str) -> dict:
        user = await self.store.get_user_by_id(user_id)
        if not user or not user.get("is_active"):
            raise AuthError("User not found", status_code=404)
        return self._public_user(user)

    async def patch_me(
        self,
        user_id: str,
        *,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        clear_email: bool = False,
    ) -> dict:
        if display_name is None and email is None and not clear_email:
            raise AuthError("No fields to update")
        display = None if display_name is None else (display_name.strip() or None)
        email_n = None if email is None else validate_email(email)
        try:
            user = await self.store.update_user(
                user_id,
                display_name=display if display_name is not None else None,
                email=email_n,
                clear_email=clear_email,
            )
        except EmailTakenError as exc:
            raise AuthError("Email is already taken", status_code=409) from exc
        if not user:
            raise AuthError("User not found", status_code=404)
        return self._public_user(user)

    async def assert_session_active(self, session_id: str, user_id: str) -> None:
        session = await self.store.get_session(session_id)
        now = datetime.now(timezone.utc)
        if (
            session is None
            or str(session["user_id"]) != str(user_id)
            or session.get("revoked_at") is not None
            or session["expires_at"] <= now
        ):
            raise AuthError("Invalid or expired token", status_code=401)
        await self.store.touch_session(session_id, now=now)
