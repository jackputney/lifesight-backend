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


async def delete_oauth_credentials(user_id: str, provider: str) -> bool:
    """Delete this user's OAuth row for a provider. Returns True if a row was removed."""
    result = await pool().execute(
        """
        DELETE FROM oauth_credentials
        WHERE user_id = $1::uuid AND provider = $2
        """,
        user_id,
        provider,
    )
    # asyncpg returns e.g. "DELETE 1"
    return result.endswith("1")


# ---------------------------------------------------------------------------
# OAuth transactions (005) — ephemeral PKCE / state (not credentials)
# ---------------------------------------------------------------------------

async def create_oauth_transaction(
    *,
    state: str,
    user_id: str,
    provider: str,
    code_verifier_enc: str,
    app_return_uri: str,
    expires_at: datetime,
) -> None:
    await pool().execute(
        """
        INSERT INTO oauth_transactions (
            state, user_id, provider, code_verifier_enc, app_return_uri, expires_at
        )
        VALUES ($1, $2::uuid, $3, $4, $5, $6)
        """,
        state,
        user_id,
        provider,
        code_verifier_enc,
        app_return_uri,
        expires_at,
    )


async def get_oauth_transaction(state: str) -> Optional[dict]:
    row = await pool().fetchrow(
        """
        SELECT state, user_id, provider, code_verifier_enc, app_return_uri,
               created_at, expires_at, consumed_at
        FROM oauth_transactions WHERE state = $1
        """,
        state,
    )
    return dict(row) if row else None


async def consume_oauth_transaction(state: str) -> Optional[dict]:
    """Atomically mark a transaction consumed. Returns the row if newly consumed."""
    row = await pool().fetchrow(
        """
        UPDATE oauth_transactions
        SET consumed_at = now()
        WHERE state = $1
          AND consumed_at IS NULL
          AND expires_at > now()
        RETURNING state, user_id, provider, code_verifier_enc, app_return_uri,
                  created_at, expires_at, consumed_at
        """,
        state,
    )
    return dict(row) if row else None


async def delete_oauth_transaction(state: str) -> None:
    await pool().execute("DELETE FROM oauth_transactions WHERE state = $1", state)


async def purge_expired_oauth_transactions() -> int:
    result = await pool().execute(
        """
        DELETE FROM oauth_transactions
        WHERE expires_at <= now() OR consumed_at IS NOT NULL
        """
    )
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0


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


# ---------------------------------------------------------------------------
# v2 Fitness (003: workout_*)
# ---------------------------------------------------------------------------

async def start_workout_session(user_id: str, plan_day_id: Optional[str] = None) -> dict:
    """Abandon any other active session for this user, then open a new one."""
    await pool().execute(
        """
        UPDATE workout_sessions
        SET status = 'abandoned', ended_at = now()
        WHERE user_id = $1::uuid AND status = 'active'
        """,
        user_id,
    )
    row = await pool().fetchrow(
        """
        INSERT INTO workout_sessions (user_id, plan_day_id, status)
        VALUES ($1::uuid, $2::uuid, 'active')
        RETURNING id, user_id, session_date, plan_day_id, status, started_at
        """,
        user_id, plan_day_id,
    )
    return dict(row)


async def get_workout_session(session_id: str, user_id: str) -> Optional[dict]:
    row = await pool().fetchrow(
        """
        SELECT id, user_id, session_date, plan_day_id, status, started_at, ended_at
        FROM workout_sessions
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        session_id, user_id,
    )
    return dict(row) if row else None


async def list_planned_exercises_for_day(plan_day_id: str) -> list[dict]:
    rows = await pool().fetch(
        """
        SELECT id, day_id, name, target_sets, target_reps, rest_seconds, sort_order
        FROM planned_exercises
        WHERE day_id = $1::uuid
        ORDER BY sort_order
        """,
        plan_day_id,
    )
    return [dict(r) for r in rows]


async def list_set_logs(session_id: str) -> list[dict]:
    rows = await pool().fetch(
        """
        SELECT id, session_id, exercise_id, set_number, reps, weight, completed_at, source
        FROM set_logs
        WHERE session_id = $1::uuid
        ORDER BY completed_at, set_number
        """,
        session_id,
    )
    return [dict(r) for r in rows]


async def insert_set_log(
    session_id: str,
    exercise_id: str,
    set_number: int,
    reps: Optional[int],
    weight: Optional[float],
    source: str = "voice",
) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO set_logs (session_id, exercise_id, set_number, reps, weight, source)
        VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)
        RETURNING id, session_id, exercise_id, set_number, reps, weight, completed_at, source
        """,
        session_id, exercise_id, set_number, reps, weight, source,
    )
    return dict(row)


async def get_personal_record(user_id: str, exercise_id: str, rep_range: int) -> Optional[dict]:
    row = await pool().fetchrow(
        """
        SELECT id, user_id, exercise_id, rep_range, weight, achieved_at
        FROM personal_records
        WHERE user_id = $1::uuid AND exercise_id = $2::uuid AND rep_range = $3
        """,
        user_id, exercise_id, rep_range,
    )
    return dict(row) if row else None


async def upsert_personal_record(
    user_id: str, exercise_id: str, rep_range: int, weight: float
) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO personal_records (user_id, exercise_id, rep_range, weight)
        VALUES ($1::uuid, $2::uuid, $3, $4)
        ON CONFLICT (user_id, exercise_id, rep_range) DO UPDATE
        SET weight = EXCLUDED.weight, achieved_at = now()
        WHERE EXCLUDED.weight > personal_records.weight
        RETURNING id, user_id, exercise_id, rep_range, weight, achieved_at
        """,
        user_id, exercise_id, rep_range, weight,
    )
    # ON CONFLICT ... WHERE can yield no row when the new weight isn't better.
    if row is None:
        existing = await get_personal_record(user_id, exercise_id, rep_range)
        return existing or {
            "user_id": user_id,
            "exercise_id": exercise_id,
            "rep_range": rep_range,
            "weight": weight,
        }
    return dict(row)


# ---------------------------------------------------------------------------
# v2 Diet (003: food_entries, daily_nutrition_targets)
# ---------------------------------------------------------------------------

async def insert_food_entry(
    user_id: str,
    *,
    method: str,
    matched_food_name: Optional[str],
    calories: Optional[float],
    protein_g: Optional[float],
    carbs_g: Optional[float],
    fat_g: Optional[float],
    confidence: Optional[float],
    raw_input_ref: Optional[str] = None,
    logged_at: Optional[datetime] = None,
) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO food_entries (
            user_id, logged_at, method, raw_input_ref, matched_food_name,
            calories, protein_g, carbs_g, fat_g, confidence
        )
        VALUES (
            $1::uuid, COALESCE($2, now()), $3, $4, $5,
            $6, $7, $8, $9, $10
        )
        RETURNING id, user_id, logged_at, method, raw_input_ref, matched_food_name,
                  calories, protein_g, carbs_g, fat_g, confidence, created_at
        """,
        user_id, logged_at, method, raw_input_ref, matched_food_name,
        calories, protein_g, carbs_g, fat_g, confidence,
    )
    return dict(row)


async def get_latest_nutrition_targets(user_id: str) -> Optional[dict]:
    row = await pool().fetchrow(
        """
        SELECT id, user_id, calories, protein_g, carbs_g, fat_g, source_upload_ref, created_at
        FROM daily_nutrition_targets
        WHERE user_id = $1::uuid
        ORDER BY created_at DESC LIMIT 1
        """,
        user_id,
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# v2 Author (003: manuscripts / chapters / scenes / brainstorm_sessions)
# ---------------------------------------------------------------------------

async def create_manuscript(user_id: str, title: str) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO manuscripts (user_id, title)
        VALUES ($1::uuid, $2)
        RETURNING id, user_id, title, created_at
        """,
        user_id, title,
    )
    return dict(row)


async def get_manuscript(manuscript_id: str, user_id: str) -> Optional[dict]:
    row = await pool().fetchrow(
        """
        SELECT id, user_id, title, created_at FROM manuscripts
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        manuscript_id, user_id,
    )
    return dict(row) if row else None


async def create_chapter(manuscript_id: str, title: str, sort_order: int) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO chapters (manuscript_id, title, sort_order)
        VALUES ($1::uuid, $2, $3)
        RETURNING id, manuscript_id, title, sort_order
        """,
        manuscript_id, title, sort_order,
    )
    return dict(row)


async def create_scene(chapter_id: str, content: str, sort_order: int) -> dict:
    words = len(content.split()) if content.strip() else 0
    row = await pool().fetchrow(
        """
        INSERT INTO scenes (chapter_id, content, word_count, sort_order)
        VALUES ($1::uuid, $2, $3, $4)
        RETURNING id, chapter_id, content, word_count, sort_order, updated_at
        """,
        chapter_id, content, words, sort_order,
    )
    return dict(row)


async def update_scene_content(scene_id: str, content: str) -> Optional[dict]:
    words = len(content.split()) if content.strip() else 0
    row = await pool().fetchrow(
        """
        UPDATE scenes
        SET content = $2, word_count = $3, updated_at = now()
        WHERE id = $1::uuid
        RETURNING id, chapter_id, content, word_count, sort_order, updated_at
        """,
        scene_id, content, words,
    )
    return dict(row) if row else None


async def get_scene(scene_id: str) -> Optional[dict]:
    row = await pool().fetchrow(
        """
        SELECT s.id, s.chapter_id, s.content, s.word_count, s.sort_order, s.updated_at,
               c.manuscript_id, c.title AS chapter_title
        FROM scenes s
        JOIN chapters c ON c.id = s.chapter_id
        WHERE s.id = $1::uuid
        """,
        scene_id,
    )
    return dict(row) if row else None


async def delete_scene(scene_id: str) -> bool:
    result = await pool().execute("DELETE FROM scenes WHERE id = $1::uuid", scene_id)
    return result == "DELETE 1"


async def list_chapters(manuscript_id: str) -> list[dict]:
    rows = await pool().fetch(
        """
        SELECT id, manuscript_id, title, sort_order FROM chapters
        WHERE manuscript_id = $1::uuid ORDER BY sort_order
        """,
        manuscript_id,
    )
    return [dict(r) for r in rows]


async def list_scenes(chapter_id: str) -> list[dict]:
    rows = await pool().fetch(
        """
        SELECT id, chapter_id, content, word_count, sort_order, updated_at
        FROM scenes WHERE chapter_id = $1::uuid ORDER BY sort_order
        """,
        chapter_id,
    )
    return [dict(r) for r in rows]


async def create_brainstorm_session(
    manuscript_id: str, transcript: str, linked_scene_id: Optional[str] = None
) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO brainstorm_sessions (manuscript_id, transcript, linked_scene_id)
        VALUES ($1::uuid, $2, $3::uuid)
        RETURNING id, manuscript_id, transcript, linked_scene_id, created_at
        """,
        manuscript_id, transcript, linked_scene_id,
    )
    return dict(row)


# ---------------------------------------------------------------------------
# v2 Wearables (003: wearable_connections, health_metrics)
# ---------------------------------------------------------------------------

async def upsert_wearable_connection(
    user_id: str, provider: str, aggregator_token_ref: Optional[str]
) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO wearable_connections (user_id, provider, aggregator_token_ref)
        VALUES ($1::uuid, $2, $3)
        ON CONFLICT (user_id, provider) DO UPDATE
        SET aggregator_token_ref = COALESCE(EXCLUDED.aggregator_token_ref, wearable_connections.aggregator_token_ref),
            connected_at = now()
        RETURNING id, user_id, provider, aggregator_token_ref, connected_at
        """,
        user_id, provider, aggregator_token_ref,
    )
    return dict(row)


async def insert_health_metric(
    user_id: str,
    *,
    metric_type: str,
    value: Optional[float],
    value_json: Any,
    source_device: Optional[str],
    recorded_at: datetime,
) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO health_metrics (
            user_id, metric_type, value, value_json, source_device, recorded_at
        )
        VALUES ($1::uuid, $2, $3, $4::jsonb, $5, $6)
        RETURNING id, user_id, metric_type, value, value_json, source_device, recorded_at, ingested_at
        """,
        user_id, metric_type, value,
        None if value_json is None else json.dumps(value_json),
        source_device, recorded_at,
    )
    return dict(row)
