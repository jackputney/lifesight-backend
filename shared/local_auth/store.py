"""User + session persistence (Postgres via shared.db, or in-memory for tests)."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from shared import db


class AuthStore(Protocol):
    async def create_user(
        self,
        *,
        username: str,
        email: Optional[str],
        password_hash: str,
        display_name: Optional[str],
    ) -> dict: ...

    async def get_user_by_id(self, user_id: str) -> Optional[dict]: ...

    async def get_user_by_username(self, username: str) -> Optional[dict]: ...

    async def get_user_by_email(self, email: str) -> Optional[dict]: ...

    async def update_user(
        self,
        user_id: str,
        *,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        clear_email: bool = False,
        password_hash: Optional[str] = None,
    ) -> Optional[dict]: ...

    async def create_session(
        self,
        *,
        user_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
        device_name: Optional[str],
    ) -> dict: ...

    async def get_session(self, session_id: str) -> Optional[dict]: ...

    async def get_session_by_refresh_hash(self, refresh_token_hash: str) -> Optional[dict]: ...

    async def rotate_session_refresh(
        self,
        session_id: str,
        *,
        new_refresh_token_hash: str,
        expires_at: datetime,
        now: datetime,
    ) -> Optional[dict]: ...

    async def touch_session(self, session_id: str, *, now: datetime) -> None: ...

    async def revoke_session(self, session_id: str, *, now: datetime) -> bool: ...

    async def revoke_all_sessions(self, user_id: str, *, now: datetime) -> int: ...


class UsernameTakenError(Exception):
    pass


class EmailTakenError(Exception):
    pass


class MemoryAuthStore:
    """Process-local store for unit tests — not for production."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self._username: dict[str, str] = {}
        self._email: dict[str, str] = {}

    def clear(self) -> None:
        self.users.clear()
        self.sessions.clear()
        self._username.clear()
        self._email.clear()

    async def create_user(
        self,
        *,
        username: str,
        email: Optional[str],
        password_hash: str,
        display_name: Optional[str],
    ) -> dict:
        if username in self._username:
            raise UsernameTakenError()
        if email and email in self._email:
            raise EmailTakenError()
        now = datetime.now(timezone.utc)
        user_id = str(uuid.uuid4())
        row = {
            "id": user_id,
            "username": username,
            "email": email,
            "password_hash": password_hash,
            "display_name": display_name,
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        self.users[user_id] = row
        self._username[username] = user_id
        if email:
            self._email[email] = user_id
        return deepcopy(row)

    async def get_user_by_id(self, user_id: str) -> Optional[dict]:
        row = self.users.get(user_id)
        return deepcopy(row) if row else None

    async def get_user_by_username(self, username: str) -> Optional[dict]:
        uid = self._username.get(username)
        return await self.get_user_by_id(uid) if uid else None

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        uid = self._email.get(email)
        return await self.get_user_by_id(uid) if uid else None

    async def update_user(
        self,
        user_id: str,
        *,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        clear_email: bool = False,
        password_hash: Optional[str] = None,
    ) -> Optional[dict]:
        row = self.users.get(user_id)
        if not row:
            return None
        if clear_email:
            old = row.get("email")
            if old:
                self._email.pop(old, None)
            row["email"] = None
        elif email is not None:
            if email in self._email and self._email[email] != user_id:
                raise EmailTakenError()
            old = row.get("email")
            if old:
                self._email.pop(old, None)
            row["email"] = email
            self._email[email] = user_id
        if display_name is not None:
            row["display_name"] = display_name
        if password_hash is not None:
            row["password_hash"] = password_hash
        row["updated_at"] = datetime.now(timezone.utc)
        return deepcopy(row)

    async def create_session(
        self,
        *,
        user_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
        device_name: Optional[str],
    ) -> dict:
        now = datetime.now(timezone.utc)
        sid = str(uuid.uuid4())
        row = {
            "id": sid,
            "user_id": user_id,
            "refresh_token_hash": refresh_token_hash,
            "expires_at": expires_at,
            "revoked_at": None,
            "created_at": now,
            "last_used_at": now,
            "device_name": device_name,
        }
        self.sessions[sid] = row
        return deepcopy(row)

    async def get_session(self, session_id: str) -> Optional[dict]:
        row = self.sessions.get(session_id)
        return deepcopy(row) if row else None

    async def get_session_by_refresh_hash(self, refresh_token_hash: str) -> Optional[dict]:
        for row in self.sessions.values():
            if row["refresh_token_hash"] == refresh_token_hash:
                return deepcopy(row)
        return None

    async def rotate_session_refresh(
        self,
        session_id: str,
        *,
        new_refresh_token_hash: str,
        expires_at: datetime,
        now: datetime,
    ) -> Optional[dict]:
        row = self.sessions.get(session_id)
        if not row or row.get("revoked_at") is not None:
            return None
        row["refresh_token_hash"] = new_refresh_token_hash
        row["expires_at"] = expires_at
        row["last_used_at"] = now
        return deepcopy(row)

    async def touch_session(self, session_id: str, *, now: datetime) -> None:
        row = self.sessions.get(session_id)
        if row:
            row["last_used_at"] = now

    async def revoke_session(self, session_id: str, *, now: datetime) -> bool:
        row = self.sessions.get(session_id)
        if not row or row.get("revoked_at") is not None:
            return False
        row["revoked_at"] = now
        return True

    async def revoke_all_sessions(self, user_id: str, *, now: datetime) -> int:
        n = 0
        for row in self.sessions.values():
            if str(row["user_id"]) == str(user_id) and row.get("revoked_at") is None:
                row["revoked_at"] = now
                n += 1
        return n


class PostgresAuthStore:
    async def create_user(
        self,
        *,
        username: str,
        email: Optional[str],
        password_hash: str,
        display_name: Optional[str],
    ) -> dict:
        import asyncpg

        try:
            return await db.create_local_user(
                username=username,
                email=email,
                password_hash=password_hash,
                display_name=display_name,
            )
        except asyncpg.UniqueViolationError as exc:
            constraint = (exc.constraint_name or "").lower()
            if "email" in constraint:
                raise EmailTakenError() from exc
            raise UsernameTakenError() from exc

    async def get_user_by_id(self, user_id: str) -> Optional[dict]:
        return await db.get_local_user_by_id(user_id)

    async def get_user_by_username(self, username: str) -> Optional[dict]:
        return await db.get_local_user_by_username(username)

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        return await db.get_local_user_by_email(email)

    async def update_user(
        self,
        user_id: str,
        *,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        clear_email: bool = False,
        password_hash: Optional[str] = None,
    ) -> Optional[dict]:
        import asyncpg

        try:
            return await db.update_local_user(
                user_id,
                display_name=display_name,
                email=email,
                clear_email=clear_email,
                password_hash=password_hash,
            )
        except asyncpg.UniqueViolationError as exc:
            raise EmailTakenError() from exc

    async def create_session(
        self,
        *,
        user_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
        device_name: Optional[str],
    ) -> dict:
        return await db.create_auth_session(
            user_id=user_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
            device_name=device_name,
        )

    async def get_session(self, session_id: str) -> Optional[dict]:
        return await db.get_auth_session(session_id)

    async def get_session_by_refresh_hash(self, refresh_token_hash: str) -> Optional[dict]:
        return await db.get_auth_session_by_refresh_hash(refresh_token_hash)

    async def rotate_session_refresh(
        self,
        session_id: str,
        *,
        new_refresh_token_hash: str,
        expires_at: datetime,
        now: datetime,
    ) -> Optional[dict]:
        return await db.rotate_auth_session_refresh(
            session_id,
            new_refresh_token_hash=new_refresh_token_hash,
            expires_at=expires_at,
            now=now,
        )

    async def touch_session(self, session_id: str, *, now: datetime) -> None:
        await db.touch_auth_session(session_id, now=now)

    async def revoke_session(self, session_id: str, *, now: datetime) -> bool:
        return await db.revoke_auth_session(session_id, now=now)

    async def revoke_all_sessions(self, user_id: str, *, now: datetime) -> int:
        return await db.revoke_all_auth_sessions(user_id, now=now)


_MEMORY = MemoryAuthStore()
_POSTGRES = PostgresAuthStore()
_USE_MEMORY = False


def use_memory_store(enabled: bool = True) -> MemoryAuthStore:
    global _USE_MEMORY
    _USE_MEMORY = enabled
    if enabled:
        _MEMORY.clear()
    return _MEMORY


def get_store() -> AuthStore:
    return _MEMORY if _USE_MEMORY else _POSTGRES


def memory_store() -> MemoryAuthStore:
    return _MEMORY
