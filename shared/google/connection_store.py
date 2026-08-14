"""In-memory google_connections store for deterministic isolation tests.

Production uses Postgres via shared.db. Toggle with use_memory_connections(True).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from shared import db

_MEMORY: dict[str, dict] = {}  # connection_id -> row
_USE_MEMORY = False


def use_memory_connections(enabled: bool = True) -> None:
    global _USE_MEMORY
    _USE_MEMORY = enabled
    if not enabled:
        _MEMORY.clear()


def clear_memory_connections() -> None:
    _MEMORY.clear()


def _active_for_user(user_id: str) -> Optional[dict]:
    for row in _MEMORY.values():
        if str(row["user_id"]) == str(user_id) and row.get("revoked_at") is None:
            return dict(row)
    return None


async def get_active_google_connection(user_id: str) -> Optional[dict]:
    if _USE_MEMORY:
        return _active_for_user(user_id)
    return await db.get_active_google_connection(user_id)


async def upsert_active_google_connection(
    *,
    user_id: str,
    google_subject: str,
    google_email: str | None,
    display_name: str | None,
    encrypted_refresh_token: str,
    granted_scopes: list[str],
) -> dict:
    if _USE_MEMORY:
        now = datetime.now(timezone.utc)
        existing = _active_for_user(user_id)
        if existing is not None:
            existing["revoked_at"] = now
            existing["updated_at"] = now
            _MEMORY[str(existing["id"])] = existing
        # Re-activate same subject if previously revoked for this user.
        for row in list(_MEMORY.values()):
            if (
                str(row["user_id"]) == str(user_id)
                and row.get("google_subject") == google_subject
            ):
                row.update(
                    {
                        "google_email": google_email,
                        "display_name": display_name,
                        "encrypted_refresh_token": encrypted_refresh_token,
                        "granted_scopes": list(granted_scopes),
                        "revoked_at": None,
                        "updated_at": now,
                        "last_refresh_at": now,
                    }
                )
                _MEMORY[str(row["id"])] = row
                return dict(row)
        cid = str(uuid.uuid4())
        row = {
            "id": cid,
            "user_id": user_id,
            "google_subject": google_subject,
            "google_email": google_email,
            "display_name": display_name,
            "encrypted_refresh_token": encrypted_refresh_token,
            "granted_scopes": list(granted_scopes),
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
            "last_refresh_at": now,
        }
        _MEMORY[cid] = row
        return dict(row)
    return await db.upsert_active_google_connection(
        user_id=user_id,
        google_subject=google_subject,
        google_email=google_email,
        display_name=display_name,
        encrypted_refresh_token=encrypted_refresh_token,
        granted_scopes=granted_scopes,
    )


async def revoke_active_google_connection(user_id: str) -> bool:
    if _USE_MEMORY:
        row = _active_for_user(user_id)
        if row is None:
            return False
        now = datetime.now(timezone.utc)
        row["revoked_at"] = now
        row["updated_at"] = now
        _MEMORY[str(row["id"])] = row
        return True
    return await db.revoke_active_google_connection(user_id)


async def touch_google_connection_refresh(connection_id: str, user_id: str) -> None:
    if _USE_MEMORY:
        row = _MEMORY.get(connection_id)
        if row and str(row["user_id"]) == str(user_id):
            row["last_refresh_at"] = datetime.now(timezone.utc)
            row["updated_at"] = row["last_refresh_at"]
            _MEMORY[connection_id] = row
        return
    await db.touch_google_connection_refresh(connection_id, user_id)


async def update_google_connection_refresh_token(
    connection_id: str, user_id: str, encrypted_refresh_token: str
) -> None:
    if _USE_MEMORY:
        row = _MEMORY.get(connection_id)
        if row and str(row["user_id"]) == str(user_id) and row.get("revoked_at") is None:
            row["encrypted_refresh_token"] = encrypted_refresh_token
            row["updated_at"] = datetime.now(timezone.utc)
            _MEMORY[connection_id] = row
        return
    await db.update_google_connection_refresh_token(
        connection_id, user_id, encrypted_refresh_token
    )


async def get_connection_by_id_for_user(
    connection_id: str, user_id: str
) -> Optional[dict]:
    """Strict ownership lookup — never returns another user's row."""
    if _USE_MEMORY:
        row = _MEMORY.get(connection_id)
        if row is None or str(row["user_id"]) != str(user_id):
            return None
        return dict(row)
    return await db.get_google_connection_for_user(connection_id, user_id)
