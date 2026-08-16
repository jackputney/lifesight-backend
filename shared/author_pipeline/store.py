"""Author capture pipeline storage — Postgres via shared.db.pool, memory for tests.

Ownership: every query scopes by user_id derived from the JWT at the router.
Request bodies never supply ownership fields.

Immutability: there is intentionally NO update or delete statement against
author_captures anywhere in this module. Captures are inserted and read only —
refinement inserts author_draft_versions rows instead. Migration 017 backs this
with a trigger that raises on any direct UPDATE or DELETE of author_captures.

Every id that reaches SQL passes normalized_uuid() first, so a malformed path
parameter returns the ordinary "not found" answer instead of raising DataError
out of a `$1::uuid` bind.
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

from shared import db

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100

# One dictation chunk. Generous for a long spoken paragraph, small enough that no
# single request can bloat the table or the refine prompt on its own.
MAX_CAPTURE_CHARS = 20_000

# Concurrent appends in separate transactions can read the same MAX(sequence)
# under READ COMMITTED and one loses the UNIQUE (session_id, sequence) race.
# Losing that race is expected, not exceptional: re-read and retry.
CAPTURE_SEQUENCE_ATTEMPTS = 5

SESSION_STATUSES = ("active", "ended")
CAPTURE_SOURCES = ("voice", "typed")
REFINEMENT_LEVELS = (
    "light_cleanup",
    "preserve_voice",
    "polish",
    "structural_rewrite",
)
DEFAULT_REFINEMENT_LEVEL = "preserve_voice"
FLAG_CATEGORIES = (
    "typo",
    "grammar",
    "repetition",
    "tangent",
    "unclear",
    "contradiction",
    "structure",
    "other",
)
FLAG_STATUSES = ("open", "accepted", "rejected", "edited", "deferred")
DECISIONS = ("accept", "reject", "edit", "defer")

# Flag status recorded for each decision verb.
DECISION_STATUS = {
    "accept": "accepted",
    "reject": "rejected",
    "edit": "edited",
    "defer": "deferred",
}


class CaptureSequenceContention(Exception):
    """Every sequence-allocation attempt lost the UNIQUE (session_id, sequence) race."""


def normalized_uuid(value: Any) -> Optional[str]:
    """Canonical UUID string, or None when the value is not a UUID at all.

    Path parameters arrive as arbitrary strings. Without this guard a malformed
    id reaches a `$1::uuid` bind, asyncpg raises DataError, and the request 500s
    instead of taking the ordinary 404 path. Deliberately duplicated in
    shared/author_persistence/store.py rather than shared between the two author
    surfaces, which otherwise import nothing from each other.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def clamp_pagination(limit: Optional[int], offset: Optional[int]) -> tuple[int, int]:
    lim = DEFAULT_PAGE_LIMIT if limit is None else int(limit)
    off = 0 if offset is None else int(offset)
    if lim < 1:
        lim = 1
    if lim > MAX_PAGE_LIMIT:
        lim = MAX_PAGE_LIMIT
    if off < 0:
        off = 0
    return lim, off


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _opt_id(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def serialize_session(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "conversation_id": _opt_id(row.get("conversation_id")),
        "manuscript_id": _opt_id(row.get("manuscript_id")),
        "title": row.get("title"),
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
        "ended_at": _iso(row.get("ended_at")),
    }


def serialize_capture(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "sequence": int(row["sequence"]),
        "source": row["source"],
        "raw_text": row["raw_text"],
        "captured_at": _iso(row["captured_at"]),
        "created_at": _iso(row["created_at"]),
    }


def serialize_draft_version(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "version": int(row["version"]),
        "refinement_level": row["refinement_level"],
        "content": row["content"],
        "source_capture_from": int(row["source_capture_from"]),
        "source_capture_to": int(row["source_capture_to"]),
        "derived_from_version_id": _opt_id(row.get("derived_from_version_id")),
        "model_identifier": row.get("model_identifier"),
        "created_at": _iso(row["created_at"]),
    }


def serialize_flag(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "draft_version_id": str(row["draft_version_id"]),
        "category": row["category"],
        "span_start": None if row.get("span_start") is None else int(row["span_start"]),
        "span_end": None if row.get("span_end") is None else int(row["span_end"]),
        "explanation": row["explanation"],
        "suggested_change": row.get("suggested_change"),
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
    }


def serialize_decision(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "flag_id": str(row["flag_id"]),
        "decision": row["decision"],
        "replacement_text": row.get("replacement_text"),
        "resulting_draft_version_id": _opt_id(row.get("resulting_draft_version_id")),
        "decided_at": _iso(row["decided_at"]),
    }


# ---------------------------------------------------------------------------
# In-memory store (tests / offline)
# ---------------------------------------------------------------------------

@dataclass
class _MemoryStore:
    sessions: dict[str, dict] = field(default_factory=dict)
    captures: dict[str, dict] = field(default_factory=dict)
    draft_versions: dict[str, dict] = field(default_factory=dict)
    flags: dict[str, dict] = field(default_factory=dict)
    flag_decisions: dict[str, dict] = field(default_factory=dict)
    # Read-only stand-ins for the conversations / manuscripts tables, so the
    # soft-reference ownership check below is exercisable without Postgres.
    conversations: dict[str, dict] = field(default_factory=dict)
    manuscripts: dict[str, dict] = field(default_factory=dict)


_memory: Optional[_MemoryStore] = None


def use_memory_store(enabled: bool = True) -> _MemoryStore:
    global _memory
    if enabled:
        _memory = _MemoryStore()
        return _memory
    _memory = None
    return _MemoryStore()


def _mem() -> Optional[_MemoryStore]:
    return _memory


def _require_memory() -> _MemoryStore:
    store = _mem()
    if store is None:
        raise RuntimeError("memory-store helper called while Postgres store is active")
    return store


def memory_seed_conversation(user_id: str, conversation_id: Optional[str] = None) -> str:
    """Memory-store only: register a conversation owned by `user_id`."""
    store = _require_memory()
    cid = conversation_id or str(uuid.uuid4())
    store.conversations[cid] = {"id": cid, "user_id": user_id}
    return cid


def memory_seed_manuscript(user_id: str, manuscript_id: Optional[str] = None) -> str:
    """Memory-store only: register a manuscript owned by `user_id`."""
    store = _require_memory()
    mid = manuscript_id or str(uuid.uuid4())
    store.manuscripts[mid] = {"id": mid, "user_id": user_id}
    return mid


# ---------------------------------------------------------------------------
# Soft references — validated at session creation, never FK-enforced
# ---------------------------------------------------------------------------

async def conversation_is_owned(conversation_id: str, user_id: str) -> bool:
    """True when the conversation exists and belongs to `user_id`."""
    canonical = normalized_uuid(conversation_id)
    if canonical is None:
        return False

    if _mem() is not None:
        row = _mem().conversations.get(canonical)
        return row is not None and row["user_id"] == user_id

    found = await db.pool().fetchval(
        "SELECT 1 FROM conversations WHERE id = $1::uuid AND user_id = $2::uuid",
        canonical, user_id,
    )
    return found is not None


async def manuscript_is_owned(manuscript_id: str, user_id: str) -> bool:
    """True when the manuscript exists and belongs to `user_id`."""
    canonical = normalized_uuid(manuscript_id)
    if canonical is None:
        return False

    if _mem() is not None:
        row = _mem().manuscripts.get(canonical)
        return row is not None and row["user_id"] == user_id

    found = await db.pool().fetchval(
        "SELECT 1 FROM manuscripts WHERE id = $1::uuid AND user_id = $2::uuid",
        canonical, user_id,
    )
    return found is not None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

async def create_session(
    user_id: str,
    *,
    title: Optional[str] = None,
    conversation_id: Optional[str] = None,
    manuscript_id: Optional[str] = None,
) -> dict:
    clean_title = title.strip() if isinstance(title, str) else None
    if clean_title == "":
        clean_title = None
    conversation_id = None if conversation_id is None else normalized_uuid(conversation_id)
    manuscript_id = None if manuscript_id is None else normalized_uuid(manuscript_id)

    if _mem() is not None:
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "conversation_id": conversation_id,
            "manuscript_id": manuscript_id,
            "title": clean_title,
            "status": "active",
            "created_at": _now(),
            "ended_at": None,
        }
        _mem().sessions[row["id"]] = row
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        INSERT INTO author_sessions (user_id, conversation_id, manuscript_id, title)
        VALUES ($1::uuid, $2::uuid, $3::uuid, $4::text)
        RETURNING id, user_id, conversation_id, manuscript_id, title, status,
                  created_at, ended_at
        """,
        user_id, conversation_id, manuscript_id, clean_title,
    )
    return dict(row)


async def get_session(session_id: str, user_id: str) -> Optional[dict]:
    session_id = normalized_uuid(session_id)
    if session_id is None:
        return None

    if _mem() is not None:
        row = _mem().sessions.get(session_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, conversation_id, manuscript_id, title, status,
               created_at, ended_at
        FROM author_sessions
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        session_id, user_id,
    )
    return dict(row) if row else None


async def list_sessions(
    user_id: str,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> tuple[list[dict], int]:
    limit, offset = clamp_pagination(limit, offset)
    if _mem() is not None:
        rows = [deepcopy(s) for s in _mem().sessions.values() if s["user_id"] == user_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[offset: offset + limit], len(rows)

    total = await db.pool().fetchval(
        "SELECT COUNT(*) FROM author_sessions WHERE user_id = $1::uuid",
        user_id,
    )
    rows = await db.pool().fetch(
        """
        SELECT id, user_id, conversation_id, manuscript_id, title, status,
               created_at, ended_at
        FROM author_sessions
        WHERE user_id = $1::uuid
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        user_id, limit, offset,
    )
    return [dict(r) for r in rows], int(total)


async def end_session(session_id: str, user_id: str) -> Optional[dict]:
    """Mark the session ended. Idempotent; keeps the original ended_at."""
    session_id = normalized_uuid(session_id)
    if session_id is None:
        return None

    if _mem() is not None:
        row = _mem().sessions.get(session_id)
        if row is None or row["user_id"] != user_id:
            return None
        if row["status"] != "ended":
            row["status"] = "ended"
            row["ended_at"] = _now()
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        UPDATE author_sessions
        SET status = 'ended',
            ended_at = COALESCE(ended_at, now())
        WHERE id = $1::uuid AND user_id = $2::uuid
        RETURNING id, user_id, conversation_id, manuscript_id, title, status,
                  created_at, ended_at
        """,
        session_id, user_id,
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Captures — INSERT and SELECT only. Never UPDATE. Never DELETE.
# ---------------------------------------------------------------------------

async def append_capture(
    session_id: str,
    user_id: str,
    *,
    source: str,
    raw_text: str,
) -> Optional[dict]:
    """Append raw dictation with the next sequence. None if session not owned.

    Raises CaptureSequenceContention if every attempt loses the sequence race.
    """
    session = await get_session(session_id, user_id)
    if session is None:
        return None
    session_id = str(session["id"])

    if _mem() is not None:
        existing = [
            c["sequence"] for c in _mem().captures.values() if c["session_id"] == session_id
        ]
        now = _now()
        row = {
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "user_id": user_id,
            "sequence": (max(existing) + 1) if existing else 0,
            "source": source,
            "raw_text": raw_text,
            "captured_at": now,
            "created_at": now,
        }
        _mem().captures[row["id"]] = row
        return deepcopy(row)

    # One statement reads MAX(sequence) and inserts, but that is NOT collision
    # proof: under READ COMMITTED two concurrent appends in separate
    # transactions read the same MAX and the loser hits
    # UNIQUE (session_id, sequence). The index is the arbiter and this loop is
    # the recovery — each retry re-reads MAX and takes the next free sequence,
    # so a voice client streaming chunks in parallel still gets gap-free
    # sequences instead of a 500.
    for _attempt in range(CAPTURE_SEQUENCE_ATTEMPTS):
        try:
            row = await db.pool().fetchrow(
                """
                INSERT INTO author_captures (session_id, user_id, sequence, source, raw_text)
                SELECT $1::uuid, $2::uuid, COALESCE(MAX(sequence) + 1, 0), $3::text, $4::text
                FROM author_captures WHERE session_id = $1::uuid
                RETURNING id, session_id, user_id, sequence, source, raw_text,
                          captured_at, created_at
                """,
                session_id, user_id, source, raw_text,
            )
        except asyncpg.UniqueViolationError:
            continue
        return dict(row) if row else None

    raise CaptureSequenceContention(
        f"sequence allocation lost {CAPTURE_SEQUENCE_ATTEMPTS} consecutive races"
    )


async def list_captures(
    session_id: str,
    user_id: str,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> Optional[tuple[list[dict], int]]:
    """Paginated provenance view. None if the session is not owned."""
    session = await get_session(session_id, user_id)
    if session is None:
        return None
    session_id = str(session["id"])
    limit, offset = clamp_pagination(limit, offset)

    if _mem() is not None:
        rows = [
            deepcopy(c) for c in _mem().captures.values()
            if c["session_id"] == session_id and c["user_id"] == user_id
        ]
        rows.sort(key=lambda r: r["sequence"])
        return rows[offset: offset + limit], len(rows)

    total = await db.pool().fetchval(
        """
        SELECT COUNT(*) FROM author_captures
        WHERE session_id = $1::uuid AND user_id = $2::uuid
        """,
        session_id, user_id,
    )
    rows = await db.pool().fetch(
        """
        SELECT id, session_id, user_id, sequence, source, raw_text,
               captured_at, created_at
        FROM author_captures
        WHERE session_id = $1::uuid AND user_id = $2::uuid
        ORDER BY sequence ASC
        LIMIT $3 OFFSET $4
        """,
        session_id, user_id, limit, offset,
    )
    return [dict(r) for r in rows], int(total)


async def all_captures(session_id: str, user_id: str) -> list[dict]:
    """Every capture in sequence order (session ownership checked by caller)."""
    session_id = normalized_uuid(session_id)
    if session_id is None:
        return []

    if _mem() is not None:
        rows = [
            deepcopy(c) for c in _mem().captures.values()
            if c["session_id"] == session_id and c["user_id"] == user_id
        ]
        rows.sort(key=lambda r: r["sequence"])
        return rows

    rows = await db.pool().fetch(
        """
        SELECT id, session_id, user_id, sequence, source, raw_text,
               captured_at, created_at
        FROM author_captures
        WHERE session_id = $1::uuid AND user_id = $2::uuid
        ORDER BY sequence ASC
        """,
        session_id, user_id,
    )
    return [dict(r) for r in rows]


async def captures_in_range(
    session_id: str,
    user_id: str,
    sequence_from: int,
    sequence_to: int,
) -> list[dict]:
    """Captures with sequence in [sequence_from, sequence_to], inclusive."""
    session_id = normalized_uuid(session_id)
    if session_id is None:
        return []

    if _mem() is not None:
        rows = [
            deepcopy(c) for c in _mem().captures.values()
            if c["session_id"] == session_id
            and c["user_id"] == user_id
            and sequence_from <= c["sequence"] <= sequence_to
        ]
        rows.sort(key=lambda r: r["sequence"])
        return rows

    rows = await db.pool().fetch(
        """
        SELECT id, session_id, user_id, sequence, source, raw_text,
               captured_at, created_at
        FROM author_captures
        WHERE session_id = $1::uuid AND user_id = $2::uuid
          AND sequence BETWEEN $3 AND $4
        ORDER BY sequence ASC
        """,
        session_id, user_id, sequence_from, sequence_to,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Draft versions (derivative, append-only)
# ---------------------------------------------------------------------------

async def list_draft_versions(session_id: str, user_id: str) -> list[dict]:
    """Newest version first (session ownership checked by caller)."""
    session_id = normalized_uuid(session_id)
    if session_id is None:
        return []

    if _mem() is not None:
        rows = [
            deepcopy(v) for v in _mem().draft_versions.values()
            if v["session_id"] == session_id and v["user_id"] == user_id
        ]
        rows.sort(key=lambda r: r["version"], reverse=True)
        return rows

    rows = await db.pool().fetch(
        """
        SELECT id, session_id, user_id, version, refinement_level, content,
               source_capture_from, source_capture_to, derived_from_version_id,
               model_identifier, created_at
        FROM author_draft_versions
        WHERE session_id = $1::uuid AND user_id = $2::uuid
        ORDER BY version DESC
        """,
        session_id, user_id,
    )
    return [dict(r) for r in rows]


async def get_draft_version(version_id: str, user_id: str) -> Optional[dict]:
    version_id = normalized_uuid(version_id)
    if version_id is None:
        return None

    if _mem() is not None:
        row = _mem().draft_versions.get(version_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        SELECT id, session_id, user_id, version, refinement_level, content,
               source_capture_from, source_capture_to, derived_from_version_id,
               model_identifier, created_at
        FROM author_draft_versions
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        version_id, user_id,
    )
    return dict(row) if row else None


def _memory_insert_version(
    session_id: str,
    user_id: str,
    *,
    refinement_level: str,
    content: str,
    source_capture_from: int,
    source_capture_to: int,
    derived_from_version_id: Optional[str],
    model_identifier: Optional[str],
) -> dict:
    existing = [
        v["version"] for v in _mem().draft_versions.values() if v["session_id"] == session_id
    ]
    row = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "user_id": user_id,
        "version": (max(existing) + 1) if existing else 1,
        "refinement_level": refinement_level,
        "content": content,
        "source_capture_from": source_capture_from,
        "source_capture_to": source_capture_to,
        "derived_from_version_id": derived_from_version_id,
        "model_identifier": model_identifier,
        "created_at": _now(),
    }
    _mem().draft_versions[row["id"]] = row
    return row


_INSERT_VERSION_SQL = """
    INSERT INTO author_draft_versions (
        session_id, user_id, version, refinement_level, content,
        source_capture_from, source_capture_to, derived_from_version_id,
        model_identifier
    )
    SELECT $1::uuid, $2::uuid, COALESCE(MAX(version) + 1, 1), $3::text, $4::text,
           $5::integer, $6::integer, $7::uuid, $8::text
    FROM author_draft_versions WHERE session_id = $1::uuid
    RETURNING id, session_id, user_id, version, refinement_level, content,
              source_capture_from, source_capture_to, derived_from_version_id,
              model_identifier, created_at
"""

_INSERT_FLAG_SQL = """
    INSERT INTO author_flags (
        session_id, draft_version_id, user_id, category,
        span_start, span_end, explanation, suggested_change
    )
    VALUES ($1::uuid, $2::uuid, $3::uuid, $4::text, $5::integer, $6::integer,
            $7::text, $8::text)
    RETURNING id, session_id, draft_version_id, user_id, category,
              span_start, span_end, explanation, suggested_change, status, created_at
"""


async def create_refinement(
    session_id: str,
    user_id: str,
    *,
    refinement_level: str,
    content: str,
    source_capture_from: int,
    source_capture_to: int,
    model_identifier: Optional[str],
    flags: list[dict],
) -> tuple[dict, list[dict]]:
    """Insert one new draft version plus its advisory flags, atomically.

    Captures are untouched: this only ever inserts derivative rows.
    """
    if _mem() is not None:
        version = _memory_insert_version(
            session_id,
            user_id,
            refinement_level=refinement_level,
            content=content,
            source_capture_from=source_capture_from,
            source_capture_to=source_capture_to,
            derived_from_version_id=None,
            model_identifier=model_identifier,
        )
        created = [
            _memory_insert_flag(session_id, version["id"], user_id, flag) for flag in flags
        ]
        return deepcopy(version), [deepcopy(f) for f in created]

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            version = await conn.fetchrow(
                _INSERT_VERSION_SQL,
                session_id, user_id, refinement_level, content,
                source_capture_from, source_capture_to, None, model_identifier,
            )
            created = []
            for flag in flags:
                row = await conn.fetchrow(
                    _INSERT_FLAG_SQL,
                    session_id, version["id"], user_id, flag["category"],
                    flag.get("span_start"), flag.get("span_end"),
                    flag["explanation"], flag.get("suggested_change"),
                )
                created.append(dict(row))
            return dict(version), created


def _memory_insert_flag(
    session_id: str,
    draft_version_id: str,
    user_id: str,
    flag: dict,
) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "draft_version_id": draft_version_id,
        "user_id": user_id,
        "category": flag["category"],
        "span_start": flag.get("span_start"),
        "span_end": flag.get("span_end"),
        "explanation": flag["explanation"],
        "suggested_change": flag.get("suggested_change"),
        "status": "open",
        "created_at": _now(),
    }
    _mem().flags[row["id"]] = row
    return row


# ---------------------------------------------------------------------------
# Flags + decisions
# ---------------------------------------------------------------------------

async def list_flags(
    session_id: str,
    user_id: str,
    *,
    status: Optional[str] = None,
) -> list[dict]:
    session_id = normalized_uuid(session_id)
    if session_id is None:
        return []

    if _mem() is not None:
        rows = [
            deepcopy(f) for f in _mem().flags.values()
            if f["session_id"] == session_id
            and f["user_id"] == user_id
            and (status is None or f["status"] == status)
        ]
        rows.sort(key=lambda r: r["created_at"])
        return rows

    if status is None:
        rows = await db.pool().fetch(
            """
            SELECT id, session_id, draft_version_id, user_id, category,
                   span_start, span_end, explanation, suggested_change,
                   status, created_at
            FROM author_flags
            WHERE session_id = $1::uuid AND user_id = $2::uuid
            ORDER BY created_at ASC
            """,
            session_id, user_id,
        )
    else:
        rows = await db.pool().fetch(
            """
            SELECT id, session_id, draft_version_id, user_id, category,
                   span_start, span_end, explanation, suggested_change,
                   status, created_at
            FROM author_flags
            WHERE session_id = $1::uuid AND user_id = $2::uuid AND status = $3
            ORDER BY created_at ASC
            """,
            session_id, user_id, status,
        )
    return [dict(r) for r in rows]


async def get_flag(flag_id: str, user_id: str) -> Optional[dict]:
    flag_id = normalized_uuid(flag_id)
    if flag_id is None:
        return None

    if _mem() is not None:
        row = _mem().flags.get(flag_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        SELECT id, session_id, draft_version_id, user_id, category,
               span_start, span_end, explanation, suggested_change,
               status, created_at
        FROM author_flags
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        flag_id, user_id,
    )
    return dict(row) if row else None


async def record_flag_decision(
    flag: dict,
    user_id: str,
    *,
    decision: str,
    replacement_text: Optional[str],
    source_version: dict,
    new_content: Optional[str],
) -> tuple[dict, dict, Optional[dict]]:
    """Resolve one flag atomically.

    `new_content` non-None inserts a NEW draft version derived from
    `source_version`; the source version and every capture stay untouched.
    Returns (flag, decision, new_draft_version | None).
    """
    new_status = DECISION_STATUS[decision]
    session_id = str(flag["session_id"])
    flag_id = str(flag["id"])

    if _mem() is not None:
        version_row: Optional[dict] = None
        if new_content is not None:
            version_row = _memory_insert_version(
                session_id,
                user_id,
                refinement_level=source_version["refinement_level"],
                content=new_content,
                source_capture_from=int(source_version["source_capture_from"]),
                source_capture_to=int(source_version["source_capture_to"]),
                derived_from_version_id=str(source_version["id"]),
                model_identifier=None,
            )
        stored_flag = _mem().flags[flag_id]
        stored_flag["status"] = new_status
        decision_row = {
            "id": str(uuid.uuid4()),
            "flag_id": flag_id,
            "user_id": user_id,
            "decision": decision,
            "replacement_text": replacement_text,
            "resulting_draft_version_id": version_row["id"] if version_row else None,
            "decided_at": _now(),
        }
        _mem().flag_decisions[decision_row["id"]] = decision_row
        return (
            deepcopy(stored_flag),
            deepcopy(decision_row),
            deepcopy(version_row) if version_row else None,
        )

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            version_row = None
            if new_content is not None:
                version_row = dict(
                    await conn.fetchrow(
                        _INSERT_VERSION_SQL,
                        session_id,
                        user_id,
                        source_version["refinement_level"],
                        new_content,
                        int(source_version["source_capture_from"]),
                        int(source_version["source_capture_to"]),
                        str(source_version["id"]),
                        None,
                    )
                )
            flag_row = await conn.fetchrow(
                """
                UPDATE author_flags
                SET status = $3::text
                WHERE id = $1::uuid AND user_id = $2::uuid
                RETURNING id, session_id, draft_version_id, user_id, category,
                          span_start, span_end, explanation, suggested_change,
                          status, created_at
                """,
                flag_id, user_id, new_status,
            )
            decision_row = await conn.fetchrow(
                """
                INSERT INTO author_flag_decisions (
                    flag_id, user_id, decision, replacement_text,
                    resulting_draft_version_id
                )
                VALUES ($1::uuid, $2::uuid, $3::text, $4::text, $5::uuid)
                RETURNING id, flag_id, user_id, decision, replacement_text,
                          resulting_draft_version_id, decided_at
                """,
                flag_id,
                user_id,
                decision,
                replacement_text,
                version_row["id"] if version_row else None,
            )
            return dict(flag_row), dict(decision_row), version_row
