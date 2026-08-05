"""Ephemeral OAuth transaction store (PKCE verifier + app return URI).

Production uses Postgres (`oauth_transactions`). Unit tests can force an
in-memory store via `use_memory_transactions(True)`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from shared import db

_MEMORY: dict[str, dict] = {}
_USE_MEMORY = False


def use_memory_transactions(enabled: bool = True) -> None:
    global _USE_MEMORY
    _USE_MEMORY = enabled
    if not enabled:
        _MEMORY.clear()


def clear_memory_transactions() -> None:
    _MEMORY.clear()


async def create(
    *,
    state: str,
    user_id: str,
    provider: str,
    code_verifier_enc: str,
    app_return_uri: str,
    expires_at: datetime,
) -> None:
    if _USE_MEMORY:
        _MEMORY[state] = {
            "state": state,
            "user_id": user_id,
            "provider": provider,
            "code_verifier_enc": code_verifier_enc,
            "app_return_uri": app_return_uri,
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
            "consumed_at": None,
        }
        return
    await db.create_oauth_transaction(
        state=state,
        user_id=user_id,
        provider=provider,
        code_verifier_enc=code_verifier_enc,
        app_return_uri=app_return_uri,
        expires_at=expires_at,
    )


async def get(state: str) -> Optional[dict]:
    if _USE_MEMORY:
        return _MEMORY.get(state)
    return await db.get_oauth_transaction(state)


async def consume(state: str) -> Optional[dict]:
    """Single-use consume. Returns None if missing, expired, or already used."""
    if _USE_MEMORY:
        row = _MEMORY.get(state)
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        exp = row["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if row["consumed_at"] is not None or exp <= now:
            return None
        row = dict(row)
        row["consumed_at"] = now
        _MEMORY[state] = row
        return row
    return await db.consume_oauth_transaction(state)


async def delete(state: str) -> None:
    if _USE_MEMORY:
        _MEMORY.pop(state, None)
        return
    await db.delete_oauth_transaction(state)
