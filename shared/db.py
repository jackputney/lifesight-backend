"""Postgres storage layer (asyncpg) — conversations, pending actions, devices.

One module owns the connection pool and every query, mirroring the pattern
shared/auth.py uses for identity: routes call these functions and never touch
SQL or the pool directly, so storage changes never touch route signatures.

Backed by migrations under migrations/*.sql. As of 007, domain user_id
columns FK public.users (self-hosted identity), not Supabase auth.users.
DATABASE_URL comes from the environment; startup fails fast with a readable
error if it's missing rather than limping along in-memory.

statement_cache_size=0 because Supabase's IPv4 connection string goes through
PgBouncer in transaction mode, which breaks asyncpg's prepared-statement
cache. Harmless on a direct connection.

Pool resilience: connection-level failures log structured diagnostics, expire
and recreate the pool once, retry idempotent reads once, and surface writes as
DatabaseUnavailableError (HTTP 503) without duplicate execution.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from typing import Any, Optional

import asyncpg

logger = logging.getLogger("lifesight.db")

_pool: Optional["ResilientPool"] = None
_pool_lock = asyncio.Lock()
_dsn: Optional[str] = None

# Set by request middleware; safe empty default outside HTTP.
request_id_var: ContextVar[str] = ContextVar("db_request_id", default="-")

# min_size=0 so process startup succeeds when the pooler is briefly unreachable;
# connections are opened on demand and reaped by max_inactive_connection_lifetime.
POOL_MIN_SIZE = 0
POOL_MAX_SIZE = 5
POOL_TIMEOUT = 10.0
POOL_COMMAND_TIMEOUT = 30.0
POOL_MAX_INACTIVE_CONNECTION_LIFETIME = 60.0

_CONNECTION_MESSAGE_MARKERS = (
    "enotfound",
    "tenant/user",
    "connection reset",
    "connection refused",
    "server closed the connection",
    "connection was closed",
    "could not connect",
    "too many connections",
    "network is unreachable",
    "name or service not known",
)


class DatabaseUnavailableError(Exception):
    """Connection-level DB failure suitable for a sanitized HTTP 503."""

    def __init__(self, message: str = "Database temporarily unavailable"):
        self.message = message
        super().__init__(message)


def _require_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy it from your Supabase project "
            "(Connect > Connection string) into .env, then run "
            "python scripts/run_migrations.py once. See README."
        )
    return dsn


def is_connection_failure(exc: BaseException) -> bool:
    """True for dead/unreachable pooler connections, not ordinary SQL errors."""
    if isinstance(
        exc,
        (
            TimeoutError,
            asyncio.TimeoutError,
            OSError,
            ConnectionError,
            asyncpg.InterfaceError,
            asyncpg.PostgresConnectionError,
            asyncpg.CannotConnectNowError,
        ),
    ):
        return True
    if isinstance(exc, asyncpg.InternalServerError):
        msg = str(exc).lower()
        return any(marker in msg for marker in _CONNECTION_MESSAGE_MARKERS)
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate and str(sqlstate).startswith("08"):
        return True
    return False


def is_idempotent_sql(query: object) -> bool:
    """Only pure SELECT statements are safe to retry after pool recovery."""
    if not isinstance(query, str):
        return False
    head = query.lstrip().split(None, 1)
    return bool(head) and head[0].upper() == "SELECT"


def pool_stats(p: Any = None) -> dict[str, Optional[int]]:
    target = p if p is not None else _pool
    raw = getattr(target, "_raw", target) if target is not None else None
    if raw is None:
        return {"pool_size": None, "idle_size": None, "max_size": None}
    try:
        return {
            "pool_size": int(raw.get_size()),
            "idle_size": int(raw.get_idle_size()),
            "max_size": int(raw.get_max_size()),
        }
    except Exception:
        return {"pool_size": None, "idle_size": None, "max_size": None}


def log_db_failure(exc: BaseException, *, request_id: Optional[str] = None) -> None:
    stats = pool_stats()
    rid = request_id if request_id is not None else request_id_var.get()
    logger.error(
        "database_failure exception_type=%s request_id=%s pool_size=%s idle_size=%s max_size=%s detail=%s",
        type(exc).__name__,
        rid,
        stats["pool_size"],
        stats["idle_size"],
        stats["max_size"],
        str(exc),
    )


async def _create_raw_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        max_inactive_connection_lifetime=POOL_MAX_INACTIVE_CONNECTION_LIFETIME,
        timeout=POOL_TIMEOUT,
        command_timeout=POOL_COMMAND_TIMEOUT,
        statement_cache_size=0,
    )


class ResilientPool:
    """Thin proxy: retry SELECT once after pool recreate; never retry writes."""

    def __init__(self, raw: asyncpg.Pool):
        self._raw = raw

    def get_size(self) -> int:
        return self._raw.get_size()

    def get_idle_size(self) -> int:
        return self._raw.get_idle_size()

    def get_max_size(self) -> int:
        return self._raw.get_max_size()

    def get_min_size(self) -> int:
        return self._raw.get_min_size()

    async def close(self) -> None:
        await self._raw.close()

    def acquire(self, *, timeout: Optional[float] = None):
        return self._acquire(timeout=timeout)

    @asynccontextmanager
    async def _acquire(self, *, timeout: Optional[float] = None):
        # Acquires are not auto-retried: callers may be mid-write transaction.
        try:
            async with self._raw.acquire(timeout=timeout) as conn:
                yield conn
        except Exception as exc:
            if not is_connection_failure(exc):
                raise
            log_db_failure(exc)
            try:
                await recreate_pool()
            except Exception as recreate_exc:
                log_db_failure(recreate_exc)
                raise DatabaseUnavailableError() from recreate_exc
            raise DatabaseUnavailableError() from exc

    async def _call(self, method_name: str, query: object, *args: Any, **kwargs: Any):
        idempotent = is_idempotent_sql(query)
        try:
            method = getattr(self._raw, method_name)
            return await method(query, *args, **kwargs)
        except Exception as exc:
            if not is_connection_failure(exc):
                raise
            log_db_failure(exc)
            try:
                await recreate_pool()
            except Exception as recreate_exc:
                log_db_failure(recreate_exc)
                raise DatabaseUnavailableError() from recreate_exc
            if not idempotent:
                # Writes: pool refreshed for later requests; do not re-execute.
                raise DatabaseUnavailableError() from exc
            try:
                fresh = pool()
                method = getattr(fresh._raw, method_name)
                return await method(query, *args, **kwargs)
            except Exception as retry_exc:
                if is_connection_failure(retry_exc):
                    log_db_failure(retry_exc)
                    raise DatabaseUnavailableError() from retry_exc
                raise

    async def fetch(self, query: object, *args: Any, **kwargs: Any):
        return await self._call("fetch", query, *args, **kwargs)

    async def fetchrow(self, query: object, *args: Any, **kwargs: Any):
        return await self._call("fetchrow", query, *args, **kwargs)

    async def fetchval(self, query: object, *args: Any, **kwargs: Any):
        return await self._call("fetchval", query, *args, **kwargs)

    async def execute(self, query: object, *args: Any, **kwargs: Any):
        return await self._call("execute", query, *args, **kwargs)

    async def executemany(self, query: object, *args: Any, **kwargs: Any):
        return await self._call("executemany", query, *args, **kwargs)


async def init_pool() -> None:
    """Create the global pool; probe once without failing app startup.

    On probe failure the API stays up in degraded mode:
    /health → 200, /health/db and DB routes → sanitized 503 until a later
    request successfully recreates/uses the pool.
    """
    global _pool, _dsn
    dsn = _require_dsn()
    _dsn = dsn
    try:
        raw = await _create_raw_pool(dsn)
    except Exception as exc:
        log_db_failure(exc)
        logger.error(
            "database_pool_init_failed request_id=%s — app starting degraded",
            request_id_var.get(),
        )
        _pool = None
        return

    _pool = ResilientPool(raw)
    try:
        # Probe the raw pool so startup does not trigger recovery/retry churn.
        await raw.fetchval("SELECT 1")
        logger.info(
            "database_pool_ready pool_size=%s idle_size=%s",
            _pool.get_size(),
            _pool.get_idle_size(),
        )
    except Exception as exc:
        if is_connection_failure(exc) or isinstance(exc, DatabaseUnavailableError):
            log_db_failure(exc)
            logger.warning(
                "database_pool_degraded request_id=%s — startup probe failed; "
                "serving /health without DB",
                request_id_var.get(),
            )
            return
        raise


async def ensure_pool() -> ResilientPool:
    """Return the pool, creating it on demand after a degraded startup."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        dsn = _dsn or _require_dsn()
        try:
            raw = await _create_raw_pool(dsn)
        except Exception as exc:
            log_db_failure(exc)
            raise DatabaseUnavailableError() from exc
        _pool = ResilientPool(raw)
        logger.warning(
            "database_pool_created_on_demand request_id=%s",
            request_id_var.get(),
        )
        return _pool


async def close_pool() -> None:
    global _pool
    async with _pool_lock:
        if _pool is not None:
            try:
                await _pool.close()
            finally:
                _pool = None


async def recreate_pool() -> None:
    """Expire the global pool and open a fresh one (one recovery path)."""
    global _pool, _dsn
    async with _pool_lock:
        dsn = _dsn or _require_dsn()
        _dsn = dsn
        old = _pool
        _pool = None
        if old is not None:
            try:
                await old.close()
            except Exception as exc:
                logger.warning(
                    "database_pool_close_failed exception_type=%s detail=%s",
                    type(exc).__name__,
                    str(exc),
                )
        raw = await _create_raw_pool(dsn)
        _pool = ResilientPool(raw)
        logger.warning(
            "database_pool_recreated request_id=%s pool_size=%s idle_size=%s",
            request_id_var.get(),
            _pool.get_size(),
            _pool.get_idle_size(),
        )


def pool() -> ResilientPool:
    if _pool is None:
        raise DatabaseUnavailableError()
    return _pool


async def check_db() -> dict[str, Any]:
    """SELECT 1 through the application pool (with read recovery)."""
    try:
        p = await ensure_pool()
    except DatabaseUnavailableError:
        raise
    value = await p.fetchval("SELECT 1")
    stats = pool_stats()
    return {"status": "ok", "result": value, **stats}


# ---------------------------------------------------------------------------
# Conversations + messages (002: conversations, messages)
# ---------------------------------------------------------------------------

async def get_conversation(conversation_id: str) -> Optional[dict]:
    row = await pool().fetchrow(
        """
        SELECT id, user_id, mode, title, started_at, last_message_at, created_at,
               summary_text, summary_through_seq
        FROM conversations WHERE id = $1::uuid
        """,
        conversation_id,
    )
    return dict(row) if row else None


async def create_conversation(
    conversation_id: str,
    user_id: str,
    mode: str,
    *,
    title: Optional[str] = None,
) -> None:
    await pool().execute(
        """
        INSERT INTO conversations (id, user_id, mode, title)
        VALUES ($1::uuid, $2::uuid, $3, $4)
        """,
        conversation_id,
        user_id,
        mode,
        title,
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


async def load_messages_with_seq(conversation_id: str) -> list[dict]:
    """Like load_messages but includes seq for summary / history APIs."""
    rows = await pool().fetch(
        """
        SELECT seq, role, content_json, created_at FROM messages
        WHERE conversation_id = $1::uuid ORDER BY seq
        """,
        conversation_id,
    )
    return [
        {
            "seq": int(r["seq"]),
            "role": r["role"],
            "content": json.loads(r["content_json"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def append_message(conversation_id: str, role: str, content: Any) -> int:
    """Append with the next seq; bump last_message_at. Returns assigned seq."""
    row = await pool().fetchrow(
        """
        INSERT INTO messages (conversation_id, role, content_json, seq)
        SELECT $1::uuid, $2, $3::jsonb, COALESCE(MAX(seq) + 1, 0)
        FROM messages WHERE conversation_id = $1::uuid
        RETURNING seq
        """,
        conversation_id,
        role,
        json.dumps(content),
    )
    await pool().execute(
        "UPDATE conversations SET last_message_at = now() WHERE id = $1::uuid",
        conversation_id,
    )
    return int(row["seq"]) if row else 0


async def set_conversation_title_if_empty(conversation_id: str, title: str) -> None:
    await pool().execute(
        """
        UPDATE conversations
        SET title = $2
        WHERE id = $1::uuid AND (title IS NULL OR BTRIM(title) = '')
        """,
        conversation_id,
        title,
    )


async def update_conversation_summary(
    conversation_id: str,
    *,
    summary_text: str,
    summary_through_seq: int,
) -> None:
    await pool().execute(
        """
        UPDATE conversations
        SET summary_text = $2, summary_through_seq = $3
        WHERE id = $1::uuid
        """,
        conversation_id,
        summary_text,
        summary_through_seq,
    )


async def insert_turn_metrics(
    conversation_id: str,
    *,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    raw_messages_included: int,
    summary_used: bool,
    summary_through_seq: Optional[int],
    approx_context_utilization: Optional[float],
    supplemental: Optional[dict] = None,
) -> None:
    await pool().execute(
        """
        INSERT INTO conversation_turn_metrics (
            conversation_id, input_tokens, output_tokens, raw_messages_included,
            summary_used, summary_through_seq, approx_context_utilization, supplemental
        )
        VALUES (
            $1::uuid, $2, $3, $4,
            $5, $6, $7, $8::jsonb
        )
        """,
        conversation_id,
        input_tokens,
        output_tokens,
        raw_messages_included,
        summary_used,
        summary_through_seq,
        approx_context_utilization,
        json.dumps(supplemental or {}),
    )


async def list_conversations(
    user_id: str,
    *,
    limit: int = 20,
    cursor: Optional[str] = None,
) -> list[dict]:
    """Cursor is last_message_at ISO + id for stable pagination (desc)."""
    limit = max(1, min(int(limit), 50))
    if cursor:
        # cursor format: "<iso_ts>|<uuid>"
        try:
            ts_raw, cid = cursor.split("|", 1)
            cursor_ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            uuid.UUID(cid)
        except (ValueError, AttributeError) as exc:
            raise ValueError("invalid cursor") from exc
        rows = await pool().fetch(
            """
            SELECT id, mode, title, started_at, last_message_at, created_at
            FROM conversations
            WHERE user_id = $1::uuid
              AND (
                    COALESCE(last_message_at, created_at) < $2::timestamptz
                 OR (
                        COALESCE(last_message_at, created_at) = $2::timestamptz
                    AND id < $3::uuid
                 )
              )
            ORDER BY COALESCE(last_message_at, created_at) DESC, id DESC
            LIMIT $4
            """,
            user_id,
            cursor_ts,
            cid,
            limit,
        )
    else:
        rows = await pool().fetch(
            """
            SELECT id, mode, title, started_at, last_message_at, created_at
            FROM conversations
            WHERE user_id = $1::uuid
            ORDER BY COALESCE(last_message_at, created_at) DESC, id DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    return [dict(r) for r in rows]


async def list_messages_page(
    conversation_id: str,
    *,
    limit: int = 50,
    before_seq: Optional[int] = None,
) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    if before_seq is None:
        rows = await pool().fetch(
            """
            SELECT seq, role, content_json, created_at FROM messages
            WHERE conversation_id = $1::uuid
            ORDER BY seq DESC
            LIMIT $2
            """,
            conversation_id,
            limit,
        )
    else:
        rows = await pool().fetch(
            """
            SELECT seq, role, content_json, created_at FROM messages
            WHERE conversation_id = $1::uuid AND seq < $2
            ORDER BY seq DESC
            LIMIT $3
            """,
            conversation_id,
            before_seq,
            limit,
        )
    # Return ascending for clients.
    ordered = list(reversed([dict(r) for r in rows]))
    for item in ordered:
        item["content"] = json.loads(item.pop("content_json"))
    return ordered


async def find_conversations_for_open(
    user_id: str,
    *,
    mode: Optional[str] = None,
    started_after: Optional[datetime] = None,
    started_before: Optional[datetime] = None,
    limit: int = 10,
) -> list[dict]:
    clauses = ["user_id = $1::uuid"]
    args: list[Any] = [user_id]
    idx = 2
    if mode:
        clauses.append(f"mode = ${idx}")
        args.append(mode)
        idx += 1
    if started_after is not None:
        clauses.append(f"COALESCE(last_message_at, created_at) >= ${idx}::timestamptz")
        args.append(started_after)
        idx += 1
    if started_before is not None:
        clauses.append(f"COALESCE(last_message_at, created_at) < ${idx}::timestamptz")
        args.append(started_before)
        idx += 1
    args.append(max(1, min(int(limit), 20)))
    sql = f"""
        SELECT id, mode, title, started_at, last_message_at, created_at
        FROM conversations
        WHERE {' AND '.join(clauses)}
        ORDER BY COALESCE(last_message_at, created_at) DESC, id DESC
        LIMIT ${idx}
    """
    rows = await pool().fetch(sql, *args)
    return [dict(r) for r in rows]


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


# ---------------------------------------------------------------------------
# Self-hosted auth (006: users, auth_sessions)
# ---------------------------------------------------------------------------

_USER_COLS = (
    "id, username, email, password_hash, display_name, is_active, created_at, updated_at"
)
_SESSION_COLS = (
    "id, user_id, refresh_token_hash, expires_at, revoked_at, "
    "created_at, last_used_at, device_name"
)


async def create_local_user(
    *,
    username: str,
    email: Optional[str],
    password_hash: str,
    display_name: Optional[str],
) -> dict:
    row = await pool().fetchrow(
        f"""
        INSERT INTO users (username, email, password_hash, display_name)
        VALUES ($1, $2, $3, $4)
        RETURNING {_USER_COLS}
        """,
        username,
        email,
        password_hash,
        display_name,
    )
    return dict(row)


async def get_local_user_by_id(user_id: str) -> Optional[dict]:
    row = await pool().fetchrow(
        f"SELECT {_USER_COLS} FROM users WHERE id = $1::uuid",
        user_id,
    )
    return dict(row) if row else None


async def get_local_user_by_username(username: str) -> Optional[dict]:
    row = await pool().fetchrow(
        f"SELECT {_USER_COLS} FROM users WHERE username = $1",
        username,
    )
    return dict(row) if row else None


async def get_local_user_by_email(email: str) -> Optional[dict]:
    row = await pool().fetchrow(
        f"SELECT {_USER_COLS} FROM users WHERE email = $1",
        email,
    )
    return dict(row) if row else None


async def update_local_user(
    user_id: str,
    *,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    clear_email: bool = False,
    password_hash: Optional[str] = None,
) -> Optional[dict]:
    # Build a dynamic UPDATE that only touches provided fields.
    sets: list[str] = ["updated_at = now()"]
    args: list[Any] = []
    idx = 1
    if display_name is not None:
        sets.append(f"display_name = ${idx}")
        args.append(display_name)
        idx += 1
    if clear_email:
        sets.append("email = NULL")
    elif email is not None:
        sets.append(f"email = ${idx}")
        args.append(email)
        idx += 1
    if password_hash is not None:
        sets.append(f"password_hash = ${idx}")
        args.append(password_hash)
        idx += 1
    args.append(user_id)
    row = await pool().fetchrow(
        f"""
        UPDATE users SET {", ".join(sets)}
        WHERE id = ${idx}::uuid
        RETURNING {_USER_COLS}
        """,
        *args,
    )
    return dict(row) if row else None


async def create_auth_session(
    *,
    user_id: str,
    refresh_token_hash: str,
    expires_at: datetime,
    device_name: Optional[str],
) -> dict:
    row = await pool().fetchrow(
        f"""
        INSERT INTO auth_sessions (user_id, refresh_token_hash, expires_at, device_name)
        VALUES ($1::uuid, $2, $3, $4)
        RETURNING {_SESSION_COLS}
        """,
        user_id,
        refresh_token_hash,
        expires_at,
        device_name,
    )
    return dict(row)


async def get_auth_session(session_id: str) -> Optional[dict]:
    row = await pool().fetchrow(
        f"SELECT {_SESSION_COLS} FROM auth_sessions WHERE id = $1::uuid",
        session_id,
    )
    return dict(row) if row else None


async def get_auth_session_by_refresh_hash(refresh_token_hash: str) -> Optional[dict]:
    row = await pool().fetchrow(
        f"SELECT {_SESSION_COLS} FROM auth_sessions WHERE refresh_token_hash = $1",
        refresh_token_hash,
    )
    return dict(row) if row else None


async def rotate_auth_session_refresh(
    session_id: str,
    *,
    new_refresh_token_hash: str,
    expires_at: datetime,
    now: datetime,
) -> Optional[dict]:
    row = await pool().fetchrow(
        f"""
        UPDATE auth_sessions
        SET refresh_token_hash = $2,
            expires_at = $3,
            last_used_at = $4
        WHERE id = $1::uuid
          AND revoked_at IS NULL
          AND expires_at > $4
        RETURNING {_SESSION_COLS}
        """,
        session_id,
        new_refresh_token_hash,
        expires_at,
        now,
    )
    return dict(row) if row else None


async def touch_auth_session(session_id: str, *, now: datetime) -> None:
    await pool().execute(
        """
        UPDATE auth_sessions SET last_used_at = $2
        WHERE id = $1::uuid AND revoked_at IS NULL
        """,
        session_id,
        now,
    )


async def revoke_auth_session(session_id: str, *, now: datetime) -> bool:
    status = await pool().execute(
        """
        UPDATE auth_sessions SET revoked_at = $2
        WHERE id = $1::uuid AND revoked_at IS NULL
        """,
        session_id,
        now,
    )
    return status.endswith("1")


async def revoke_all_auth_sessions(user_id: str, *, now: datetime) -> int:
    status = await pool().execute(
        """
        UPDATE auth_sessions SET revoked_at = $2
        WHERE user_id = $1::uuid AND revoked_at IS NULL
        """,
        user_id,
        now,
    )
    # asyncpg returns e.g. "UPDATE 3"
    try:
        return int(status.split()[-1])
    except (IndexError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# user_profiles + admin_audit_log (012)
# ---------------------------------------------------------------------------

def _json_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return list(value)


async def get_user_display_name(user_id: str) -> Optional[str]:
    row = await pool().fetchrow(
        "SELECT display_name FROM users WHERE id = $1::uuid",
        user_id,
    )
    if not row:
        return None
    return row["display_name"]


async def get_user_profile_row(user_id: str) -> Optional[dict]:
    row = await pool().fetchrow(
        "SELECT * FROM user_profiles WHERE user_id = $1::uuid",
        user_id,
    )
    return dict(row) if row else None


_JSONB_PROFILE_KEYS = frozenset(
    {
        "primary_goals",
        "available_equipment",
        "dietary_preferences",
        "allergies_restrictions",
        "interests",
    }
)

_PROFILE_COLUMNS = frozenset(
    {
        "timezone",
        "date_of_birth",
        "height_cm",
        "weight_kg",
        "interaction_style",
        "vision_preference",
        "spoken_response_preference",
        "experience_level",
        "primary_goals",
        "training_frequency",
        "available_equipment",
        "injuries_limitations",
        "nutrition_goal",
        "dietary_preferences",
        "allergies_restrictions",
        "preferred_units",
        "training_environment",
        "typical_session_minutes",
        "sex_for_physiological_calculations",
        "occupation",
        "industry",
        "education_context",
        "interests",
        "typical_schedule",
    }
)


def _profile_jsonb_arg(value: Any) -> str:
    """Serialize list values for `$n::text::jsonb` binds (never store a JSON string scalar)."""
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("profile jsonb fields must be arrays")
        return json.dumps(parsed)
    if not isinstance(value, list):
        raise ValueError("profile jsonb fields must be arrays")
    return json.dumps(value)


async def upsert_user_profile(user_id: str, updates: dict[str, Any]) -> dict:
    """Insert or patch user_profiles. `updates` keys are column names."""
    clean = {k: v for k, v in updates.items() if k in _PROFILE_COLUMNS}
    for key in _JSONB_PROFILE_KEYS:
        if key in clean and clean[key] is not None:
            clean[key] = _profile_jsonb_arg(clean[key])

    existing = await get_user_profile_row(user_id)
    if existing is None:
        if not clean:
            row = await pool().fetchrow(
                """
                INSERT INTO user_profiles (user_id) VALUES ($1::uuid)
                RETURNING *
                """,
                user_id,
            )
            return dict(row)
        cols = ["user_id", *clean.keys()]
        ph: list[str] = ["$1::uuid"]
        args: list[Any] = [user_id]
        for i, key in enumerate(clean.keys(), start=2):
            # text→jsonb so a Python str bind parses as JSON, not a jsonb string.
            ph.append(f"${i}::text::jsonb" if key in _JSONB_PROFILE_KEYS else f"${i}")
            args.append(clean[key])
        row = await pool().fetchrow(
            f"""
            INSERT INTO user_profiles ({', '.join(cols)})
            VALUES ({', '.join(ph)})
            RETURNING *
            """,
            *args,
        )
        return dict(row)

    if not clean:
        return existing
    sets: list[str] = []
    args: list[Any] = [user_id]
    for i, (key, value) in enumerate(clean.items(), start=2):
        sets.append(
            f"{key} = ${i}::text::jsonb"
            if key in _JSONB_PROFILE_KEYS
            else f"{key} = ${i}"
        )
        args.append(value)
    sets.append("updated_at = now()")
    row = await pool().fetchrow(
        f"""
        UPDATE user_profiles SET {', '.join(sets)}
        WHERE user_id = $1::uuid
        RETURNING *
        """,
        *args,
    )
    return dict(row) if row else existing


async def find_user_for_seed(
    *,
    user_id: Optional[str] = None,
    username: Optional[str] = None,
    email: Optional[str] = None,
) -> Optional[dict]:
    if user_id:
        row = await pool().fetchrow(
            "SELECT id, username, email, display_name FROM users WHERE id = $1::uuid",
            user_id,
        )
    elif username:
        row = await pool().fetchrow(
            "SELECT id, username, email, display_name FROM users WHERE username = $1",
            username.strip().lower(),
        )
    elif email:
        row = await pool().fetchrow(
            "SELECT id, username, email, display_name FROM users WHERE email = $1",
            email.strip().lower(),
        )
    else:
        return None
    return dict(row) if row else None


async def insert_admin_audit(
    *,
    actor: str,
    action: str,
    target_user_id: Optional[str],
    detail: Optional[dict] = None,
) -> None:
    await pool().execute(
        """
        INSERT INTO admin_audit_log (actor, action, target_user_id, detail)
        VALUES ($1, $2, $3::uuid, $4::jsonb)
        """,
        actor,
        action,
        target_user_id,
        json.dumps(detail or {}),
    )


# ---------------------------------------------------------------------------
# daily_checkins (013)
# ---------------------------------------------------------------------------

_DAILY_CHECKIN_FIELDS = frozenset(
    {
        "sleep_hours",
        "sleep_quality",
        "energy",
        "mood",
        "stress",
        "soreness",
        "top_priority",
        "notes",
        "summary",
    }
)


async def get_daily_checkin(user_id: str, local_date: date) -> Optional[dict]:
    row = await pool().fetchrow(
        """
        SELECT * FROM daily_checkins
        WHERE user_id = $1::uuid AND local_date = $2
        """,
        user_id,
        local_date,
    )
    return dict(row) if row else None


async def upsert_daily_checkin_start(
    *,
    user_id: str,
    local_date: date,
    timezone_name: str,
    conversation_id: str,
) -> dict:
    """Create or promote today's check-in to in_progress (never reopen completed)."""
    existing = await get_daily_checkin(user_id, local_date)
    if existing is not None and existing.get("status") == "completed":
        return existing

    row = await pool().fetchrow(
        """
        INSERT INTO daily_checkins (
            user_id, local_date, timezone, conversation_id, status, started_at
        )
        VALUES ($1::uuid, $2, $3, $4::uuid, 'in_progress', now())
        ON CONFLICT (user_id, local_date) DO UPDATE SET
            timezone = EXCLUDED.timezone,
            conversation_id = COALESCE(
                daily_checkins.conversation_id, EXCLUDED.conversation_id
            ),
            status = CASE
                WHEN daily_checkins.status = 'completed' THEN daily_checkins.status
                ELSE 'in_progress'
            END,
            started_at = COALESCE(daily_checkins.started_at, now()),
            updated_at = now()
        RETURNING *
        """,
        user_id,
        local_date,
        timezone_name,
        conversation_id,
    )
    return dict(row)


async def update_daily_checkin_fields(
    *,
    user_id: str,
    local_date: date,
    timezone_name: str,
    conversation_id: str,
    fields: dict[str, Any],
    mark_completed: bool = False,
) -> Optional[dict]:
    """Patch structured fields on today's check-in; optionally mark completed."""
    clean = {k: v for k, v in fields.items() if k in _DAILY_CHECKIN_FIELDS}
    existing = await get_daily_checkin(user_id, local_date)
    if existing is None:
        await upsert_daily_checkin_start(
            user_id=user_id,
            local_date=local_date,
            timezone_name=timezone_name,
            conversation_id=conversation_id,
        )

    sets: list[str] = []
    args: list[Any] = [user_id, local_date]
    idx = 3
    for key, value in clean.items():
        sets.append(f"{key} = ${idx}")
        args.append(value)
        idx += 1
    if mark_completed:
        sets.append("status = 'completed'")
        sets.append("completed_at = COALESCE(completed_at, now())")
    elif clean:
        sets.append(
            "status = CASE WHEN status = 'completed' THEN status ELSE 'in_progress' END"
        )
    if not sets:
        return await get_daily_checkin(user_id, local_date)

    sets.append("updated_at = now()")
    if conversation_id:
        sets.append(f"conversation_id = COALESCE(conversation_id, ${idx}::uuid)")
        args.append(conversation_id)
        idx += 1

    row = await pool().fetchrow(
        f"""
        UPDATE daily_checkins SET {', '.join(sets)}
        WHERE user_id = $1::uuid AND local_date = $2
        RETURNING *
        """,
        *args,
    )
    return dict(row) if row else None

