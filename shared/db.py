"""Postgres storage layer (asyncpg) — conversations, pending actions, devices.

One module owns the connection pool and every query, mirroring the pattern
shared/auth.py uses for identity: routes call these functions and never touch
SQL or the pool directly, so storage changes never touch route signatures.

Backed by migrations/001_users_devices.sql and 002_core_schema.sql, run
against a Supabase project (the schema FKs auth.users, which only exists
there). DATABASE_URL comes from the environment; startup fails fast with a
readable error if it's missing rather than limping along in-memory.

statement_cache_size=0 because Supabase's IPv4 connection string goes through
PgBouncer in transaction mode, which breaks asyncpg's prepared-statement
cache. Harmless on a direct connection.
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> None:
    global _pool
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy it from your Supabase project "
            "(Connect > Connection string) into .env, then run "
            "python scripts/run_migrations.py once. See README."
        )
    _pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=5, statement_cache_size=0
    )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — did app startup run?")
    return _pool


# ---------------------------------------------------------------------------
# Conversations + messages (002: conversations, messages)
# ---------------------------------------------------------------------------

async def get_conversation(conversation_id: str) -> Optional[dict]:
    row = await pool().fetchrow(
        "SELECT id, user_id, mode FROM conversations WHERE id = $1::uuid",
        conversation_id,
    )
    return dict(row) if row else None


async def create_conversation(conversation_id: str, user_id: str, mode: str) -> None:
    await pool().execute(
        """
        INSERT INTO conversations (id, user_id, mode)
        VALUES ($1::uuid, $2::uuid, $3)
        """,
        conversation_id, user_id, mode,
    )


async def load_messages(conversation_id: str) -> list[dict]:
    """History in Anthropic messages shape, ordered by seq."""
    rows = await pool().fetch(
        """
        SELECT role, content_json FROM messages
        WHERE conversation_id = $1::uuid ORDER BY seq
        """,
        conversation_id,
    )
    return [{"role": r["role"], "content": json.loads(r["content_json"])} for r in rows]


async def append_message(conversation_id: str, role: str, content: Any) -> None:
    """Append with the next seq for this conversation and bump last_message_at.

    content is stored exactly as it appears in the Anthropic messages array
    (a string today; content-block lists later when tool-calling lands).
    """
    await pool().execute(
        """
        INSERT INTO messages (conversation_id, role, content_json, seq)
        SELECT $1::uuid, $2, $3::jsonb, COALESCE(MAX(seq) + 1, 0)
        FROM messages WHERE conversation_id = $1::uuid
        """,
        conversation_id, role, json.dumps(content),
    )
    await pool().execute(
        "UPDATE conversations SET last_message_at = now() WHERE id = $1::uuid",
        conversation_id,
    )


# ---------------------------------------------------------------------------
# Pending actions — the Confirm Gate (002: pending_actions)
# ---------------------------------------------------------------------------

async def create_pending_action(
    user_id: str,
    conversation_id: Optional[str],
    source_mode: str,
    action_type: str,
    payload: Any,
    description: str,
    expires_at: datetime,
) -> str:
    """Insert a pending confirm-gate row; returns the new action id."""
    row = await pool().fetchrow(
        """
        INSERT INTO pending_actions (
            user_id, conversation_id, source_mode, action_type,
            payload, description, expires_at
        )
        VALUES (
            $1::uuid, $2::uuid, $3, $4,
            $5::jsonb, $6, $7
        )
        RETURNING id
        """,
        user_id,
        conversation_id,
        source_mode,
        action_type,
        json.dumps(payload),
        description,
        expires_at,
    )
    return str(row["id"])


async def get_pending_action(action_id: str) -> Optional[dict]:
    try:
        uuid.UUID(action_id)
    except ValueError:
        return None  # malformed id can't exist; /confirm 404s the same either way
    row = await pool().fetchrow(
        """
        SELECT id, user_id, conversation_id, source_mode, action_type,
               payload, description, status, expires_at
        FROM pending_actions WHERE id = $1::uuid
        """,
        action_id,
    )
    if row is None:
        return None
    result = dict(row)
    # payload comes back as raw jsonb text, same as content_json in
    # load_messages — decode it here so every caller gets a plain dict.
    result["payload"] = json.loads(result["payload"]) if result["payload"] else None
    return result


async def resolve_pending_action(
    action_id: str, status: str, confirmed_via: Optional[str] = None
) -> None:
    """Set final status (confirmed/rejected/expired) and stamp resolved_at."""
    await pool().execute(
        """
        UPDATE pending_actions
        SET status = $2, confirmed_via = $3, resolved_at = $4
        WHERE id = $1::uuid
        """,
        action_id, status, confirmed_via, datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Memories (002: memories)
# ---------------------------------------------------------------------------

async def save_memory(user_id: str, content: str) -> str:
    """Store a long-term memory; returns the new memory id."""
    row = await pool().fetchrow(
        """
        INSERT INTO memories (user_id, content)
        VALUES ($1::uuid, $2)
        RETURNING id
        """,
        user_id, content,
    )
    return str(row["id"])


def _query_tokens(query: str) -> list[str]:
    return [t.lower() for t in re.findall(r"\w+", query)]


async def recall_memories(
    user_id: str, query: str, limit: int = 20
) -> list[dict]:
    """Token-match memories for this user; ranked by hit count."""
    tokens = _query_tokens(query)
    if not tokens:
        return []

    rows = await pool().fetch(
        """
        SELECT id, user_id, content, created_at
        FROM memories WHERE user_id = $1::uuid
        """,
        user_id,
    )

    scored: list[tuple[int, dict]] = []
    for row in rows:
        content = (row["content"] or "").lower()
        hits = sum(1 for token in tokens if token in content)
        if hits:
            scored.append((hits, dict(row)))

    scored.sort(key=lambda item: (item[0], str(item[1]["id"])), reverse=True)
    return [row for _, row in scored[:limit]]


# ---------------------------------------------------------------------------
# Action log (002: action_log)
# ---------------------------------------------------------------------------

async def log_action(
    user_id: str,
    mode: str,
    tool_name: str,
    args: Any,
    result_summary: Optional[str],
    confirmed: bool,
) -> None:
    await pool().execute(
        """
        INSERT INTO action_log (
            user_id, mode, tool_name, args_json, result_summary, confirmed
        )
        VALUES ($1::uuid, $2, $3, $4::jsonb, $5, $6)
        """,
        user_id, mode, tool_name, json.dumps(args), result_summary, confirmed,
    )


# ---------------------------------------------------------------------------
# Devices (001: devices)
# ---------------------------------------------------------------------------

async def upsert_device(
    user_id: str, device_id: str, push_token: Optional[str], platform: str
) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO devices (device_id, user_id, push_token, platform, last_seen)
        VALUES ($1, $2::uuid, $3, $4, now())
        ON CONFLICT (user_id, device_id) DO UPDATE
        SET push_token = EXCLUDED.push_token,
            platform   = EXCLUDED.platform,
            last_seen  = now()
        RETURNING device_id, user_id, push_token, platform, last_seen
        """,
        device_id, user_id, push_token, platform,
    )
    return dict(row)


async def list_devices(user_id: str) -> list[dict]:
    rows = await pool().fetch(
        """
        SELECT device_id, user_id, push_token, platform, last_seen
        FROM devices WHERE user_id = $1::uuid ORDER BY last_seen DESC
        """,
        user_id,
    )
    return [dict(r) for r in rows]


async def delete_device(user_id: str, device_id: str) -> bool:
    result = await pool().execute(
        "DELETE FROM devices WHERE user_id = $1::uuid AND device_id = $2",
        user_id, device_id,
    )
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# Google OAuth credentials (002: oauth_credentials)
# ---------------------------------------------------------------------------
# Tokens in access_token_enc/refresh_token_enc are opaque ciphertext to this
# module — callers encrypt/decrypt with shared/crypto.py. This function never
# sees or logs plaintext.

async def save_oauth_credentials(
    user_id: str,
    provider: str,
    access_token_enc: str,
    refresh_token_enc: Optional[str],
    scopes: list[str],
    expires_at: Optional[datetime],
) -> None:
    """Upsert this user's OAuth credentials for a provider. A re-consent that
    comes back without a new refresh token (Google only issues one on first
    consent) keeps the previously stored one rather than nulling it out."""
    await pool().execute(
        """
        INSERT INTO oauth_credentials (
            user_id, provider, access_token_enc, refresh_token_enc, scopes, expires_at
        )
        VALUES ($1::uuid, $2, $3, $4, $5, $6)
        ON CONFLICT (user_id, provider) DO UPDATE
        SET access_token_enc = EXCLUDED.access_token_enc,
            refresh_token_enc = COALESCE(EXCLUDED.refresh_token_enc, oauth_credentials.refresh_token_enc),
            scopes = EXCLUDED.scopes,
            expires_at = EXCLUDED.expires_at,
            updated_at = now()
        """,
        user_id, provider, access_token_enc, refresh_token_enc, scopes, expires_at,
    )


async def get_oauth_credentials(user_id: str, provider: str = "google") -> Optional[dict]:
    """Returns the row with tokens still encrypted — caller decrypts."""
    row = await pool().fetchrow(
        """
        SELECT user_id, provider, access_token_enc, refresh_token_enc, scopes, expires_at
        FROM oauth_credentials WHERE user_id = $1::uuid AND provider = $2
        """,
        user_id, provider,
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Writing documents (002: writing_documents) — Google Docs is source of truth
# ---------------------------------------------------------------------------

async def create_writing_document(
    user_id: str, google_doc_id: str, title: str, doc_type: str = "manuscript"
) -> str:
    """Register a Google Doc as this user's tracked writing document. Returns
    the new writing_documents row id."""
    row = await pool().fetchrow(
        """
        INSERT INTO writing_documents (user_id, google_doc_id, title, doc_type)
        VALUES ($1::uuid, $2, $3, $4)
        RETURNING id
        """,
        user_id, google_doc_id, title, doc_type,
    )
    return str(row["id"])


async def get_writing_document(user_id: str, doc_type: str = "manuscript") -> Optional[dict]:
    """Most recently created non-deleted document of this type for the user.
    One document per type today; picking among several is a future feature,
    not a bug in this query."""
    row = await pool().fetchrow(
        """
        SELECT id, user_id, google_doc_id, title, doc_type, last_known_revision_id
        FROM writing_documents
        WHERE user_id = $1::uuid AND doc_type = $2 AND deleted_at IS NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        user_id, doc_type,
    )
    return dict(row) if row else None


async def update_writing_document_revision(document_id: str, revision_id: str) -> None:
    """Stamp the latest known Docs revisionId after a write, so a future read
    can detect the doc changed elsewhere (a web edit) since our last write."""
    await pool().execute(
        "UPDATE writing_documents SET last_known_revision_id = $2, updated_at = now() WHERE id = $1::uuid",
        document_id, revision_id,
    )
