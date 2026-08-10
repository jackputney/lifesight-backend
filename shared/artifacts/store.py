"""Artifact persistence — Postgres via shared.db.pool, optional memory for tests.

Ownership: every query scopes by user_id derived from the JWT at the router.
Request bodies never supply ownership fields.
"""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared import db

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100


class ConflictError(Exception):
    """Optimistic concurrency failure (stale expected_revision)."""

    def __init__(self, current: dict):
        self.current = current
        super().__init__("Artifact revision conflict")


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


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _as_object(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise TypeError("JSON object required")
        return parsed
    if isinstance(value, (bytes, memoryview)):
        parsed = json.loads(bytes(value))
        if not isinstance(parsed, dict):
            raise TypeError("JSON object required")
        return parsed
    raise TypeError(f"Unsupported JSONB value type: {type(value)!r}")


def serialize_artifact(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "type": row["type"],
        "title": row["title"],
        "content": _as_object(row["content"]),
        "revision": int(row["revision"]),
        "metadata": _as_object(row.get("metadata")),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def serialize_version(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "artifact_id": str(row["artifact_id"]),
        "revision": int(row["revision"]),
        "title": row["title"],
        "content": _as_object(row["content"]),
        "metadata": _as_object(row.get("metadata")),
        "created_at": _iso(row["created_at"]),
    }


# ---------------------------------------------------------------------------
# In-memory store (tests)
# ---------------------------------------------------------------------------

@dataclass
class _MemoryStore:
    artifacts: dict[str, dict] = field(default_factory=dict)
    versions: dict[str, dict] = field(default_factory=dict)


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


def _insert_version_memory(
    artifact_id: str,
    user_id: str,
    revision: int,
    title: str,
    content: dict,
    metadata: dict,
    *,
    created_at: Optional[datetime] = None,
) -> dict:
    now = created_at or _now()
    row = {
        "id": str(uuid.uuid4()),
        "artifact_id": artifact_id,
        "user_id": user_id,
        "revision": revision,
        "title": title,
        "content": deepcopy(content),
        "metadata": deepcopy(metadata),
        "created_at": now,
    }
    _mem().versions[row["id"]] = row
    return deepcopy(row)


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

async def create_artifact(
    user_id: str,
    *,
    type: str,
    title: str,
    content: Optional[dict] = None,
    metadata: Optional[dict] = None,
) -> dict:
    type_val = type.strip()
    title_val = title.strip()
    content_val = {} if content is None else dict(content)
    metadata_val = {} if metadata is None else dict(metadata)

    if _mem() is not None:
        now = _now()
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "type": type_val,
            "title": title_val,
            "content": content_val,
            "revision": 1,
            "metadata": metadata_val,
            "created_at": now,
            "updated_at": now,
        }
        _mem().artifacts[row["id"]] = row
        _insert_version_memory(
            row["id"], user_id, 1, title_val, content_val, metadata_val, created_at=now
        )
        return deepcopy(row)

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO artifacts (user_id, type, title, content, revision, metadata)
                VALUES ($1::uuid, $2, $3, $4::jsonb, 1, $5::jsonb)
                RETURNING id, user_id, type, title, content, revision, metadata,
                          created_at, updated_at
                """,
                user_id,
                type_val,
                title_val,
                json.dumps(content_val),
                json.dumps(metadata_val),
            )
            await conn.execute(
                """
                INSERT INTO artifact_versions
                    (artifact_id, user_id, revision, title, content, metadata)
                VALUES ($1::uuid, $2::uuid, 1, $3, $4::jsonb, $5::jsonb)
                """,
                row["id"],
                user_id,
                title_val,
                json.dumps(content_val),
                json.dumps(metadata_val),
            )
            return dict(row)


async def get_artifact(artifact_id: str, user_id: str) -> Optional[dict]:
    if _mem() is not None:
        row = _mem().artifacts.get(artifact_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, type, title, content, revision, metadata, created_at, updated_at
        FROM artifacts
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        artifact_id,
        user_id,
    )
    return dict(row) if row else None


async def list_artifacts(
    user_id: str,
    *,
    type: Optional[str] = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> tuple[list[dict], int]:
    limit, offset = clamp_pagination(limit, offset)
    type_filter = type.strip() if type else None

    if _mem() is not None:
        rows = [
            deepcopy(a)
            for a in _mem().artifacts.values()
            if a["user_id"] == user_id
            and (type_filter is None or a["type"] == type_filter)
        ]
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        return rows[offset: offset + limit], len(rows)

    if type_filter is None:
        total = await db.pool().fetchval(
            "SELECT COUNT(*) FROM artifacts WHERE user_id = $1::uuid",
            user_id,
        )
        rows = await db.pool().fetch(
            """
            SELECT id, user_id, type, title, content, revision, metadata,
                   created_at, updated_at
            FROM artifacts
            WHERE user_id = $1::uuid
            ORDER BY updated_at DESC
            LIMIT $2 OFFSET $3
            """,
            user_id,
            limit,
            offset,
        )
    else:
        total = await db.pool().fetchval(
            """
            SELECT COUNT(*) FROM artifacts
            WHERE user_id = $1::uuid AND type = $2
            """,
            user_id,
            type_filter,
        )
        rows = await db.pool().fetch(
            """
            SELECT id, user_id, type, title, content, revision, metadata,
                   created_at, updated_at
            FROM artifacts
            WHERE user_id = $1::uuid AND type = $2
            ORDER BY updated_at DESC
            LIMIT $3 OFFSET $4
            """,
            user_id,
            type_filter,
            limit,
            offset,
        )
    return [dict(r) for r in rows], int(total)


async def update_artifact(
    artifact_id: str,
    user_id: str,
    *,
    expected_revision: int,
    title: Optional[str] = None,
    content: Optional[dict] = None,
    metadata: Optional[dict] = None,
) -> Optional[dict]:
    """Optimistic update. Raises ConflictError on revision mismatch. None if not found."""
    current = await get_artifact(artifact_id, user_id)
    if current is None:
        return None
    if int(current["revision"]) != int(expected_revision):
        raise ConflictError(current)

    new_title = current["title"] if title is None else title.strip()
    new_content = (
        _as_object(current["content"]) if content is None else dict(content)
    )
    new_metadata = (
        _as_object(current.get("metadata")) if metadata is None else dict(metadata)
    )
    new_revision = int(current["revision"]) + 1

    if _mem() is not None:
        row = _mem().artifacts[artifact_id]
        row["title"] = new_title
        row["content"] = new_content
        row["metadata"] = new_metadata
        row["revision"] = new_revision
        row["updated_at"] = _now()
        _insert_version_memory(
            artifact_id, user_id, new_revision, new_title, new_content, new_metadata
        )
        return deepcopy(row)

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE artifacts
                SET title = $3,
                    content = $4::jsonb,
                    metadata = $5::jsonb,
                    revision = $6,
                    updated_at = now()
                WHERE id = $1::uuid AND user_id = $2::uuid AND revision = $7
                RETURNING id, user_id, type, title, content, revision, metadata,
                          created_at, updated_at
                """,
                artifact_id,
                user_id,
                new_title,
                json.dumps(new_content),
                json.dumps(new_metadata),
                new_revision,
                expected_revision,
            )
            if row is None:
                fresh = await conn.fetchrow(
                    """
                    SELECT id, user_id, type, title, content, revision, metadata,
                           created_at, updated_at
                    FROM artifacts
                    WHERE id = $1::uuid AND user_id = $2::uuid
                    """,
                    artifact_id,
                    user_id,
                )
                if fresh is None:
                    return None
                raise ConflictError(dict(fresh))
            await conn.execute(
                """
                INSERT INTO artifact_versions
                    (artifact_id, user_id, revision, title, content, metadata)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, $6::jsonb)
                """,
                artifact_id,
                user_id,
                new_revision,
                new_title,
                json.dumps(new_content),
                json.dumps(new_metadata),
            )
            return dict(row)


async def delete_artifact(artifact_id: str, user_id: str) -> bool:
    """Hard-delete artifact; versions CASCADE (DB) or are removed in memory."""
    if _mem() is not None:
        row = _mem().artifacts.get(artifact_id)
        if row is None or row["user_id"] != user_id:
            return False
        for vid in [
            v["id"]
            for v in _mem().versions.values()
            if v["artifact_id"] == artifact_id
        ]:
            del _mem().versions[vid]
        del _mem().artifacts[artifact_id]
        return True

    result = await db.pool().execute(
        "DELETE FROM artifacts WHERE id = $1::uuid AND user_id = $2::uuid",
        artifact_id,
        user_id,
    )
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# Versions (append-only)
# ---------------------------------------------------------------------------

async def create_version_snapshot(artifact_id: str, user_id: str) -> Optional[dict]:
    """Checkpoint current head without mutating artifact.revision.

    Idempotent for a given revision: if a version row already exists for the
    current head revision, return it.
    """
    current = await get_artifact(artifact_id, user_id)
    if current is None:
        return None

    revision = int(current["revision"])
    title = current["title"]
    content = _as_object(current["content"])
    metadata = _as_object(current.get("metadata"))

    if _mem() is not None:
        for ver in _mem().versions.values():
            if (
                ver["artifact_id"] == artifact_id
                and ver["user_id"] == user_id
                and int(ver["revision"]) == revision
            ):
                return deepcopy(ver)
        return _insert_version_memory(
            artifact_id, user_id, revision, title, content, metadata
        )

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            # Re-read under transaction for ownership; do not bump revision.
            head = await conn.fetchrow(
                """
                SELECT id, user_id, type, title, content, revision, metadata,
                       created_at, updated_at
                FROM artifacts
                WHERE id = $1::uuid AND user_id = $2::uuid
                FOR UPDATE
                """,
                artifact_id,
                user_id,
            )
            if head is None:
                return None
            revision = int(head["revision"])
            existing = await conn.fetchrow(
                """
                SELECT id, artifact_id, user_id, revision, title, content, metadata,
                       created_at
                FROM artifact_versions
                WHERE artifact_id = $1::uuid AND user_id = $2::uuid AND revision = $3
                """,
                artifact_id,
                user_id,
                revision,
            )
            if existing is not None:
                return dict(existing)
            ver = await conn.fetchrow(
                """
                INSERT INTO artifact_versions
                    (artifact_id, user_id, revision, title, content, metadata)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, $6::jsonb)
                RETURNING id, artifact_id, user_id, revision, title, content, metadata,
                          created_at
                """,
                artifact_id,
                user_id,
                revision,
                head["title"],
                json.dumps(_as_object(head["content"])),
                json.dumps(_as_object(head.get("metadata"))),
            )
            return dict(ver)


async def list_versions(
    artifact_id: str,
    user_id: str,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> Optional[tuple[list[dict], int]]:
    if await get_artifact(artifact_id, user_id) is None:
        return None
    limit, offset = clamp_pagination(limit, offset)

    if _mem() is not None:
        rows = [
            deepcopy(v)
            for v in _mem().versions.values()
            if v["artifact_id"] == artifact_id and v["user_id"] == user_id
        ]
        rows.sort(key=lambda r: r["revision"], reverse=True)
        return rows[offset: offset + limit], len(rows)

    total = await db.pool().fetchval(
        """
        SELECT COUNT(*) FROM artifact_versions
        WHERE artifact_id = $1::uuid AND user_id = $2::uuid
        """,
        artifact_id,
        user_id,
    )
    rows = await db.pool().fetch(
        """
        SELECT id, artifact_id, user_id, revision, title, content, metadata, created_at
        FROM artifact_versions
        WHERE artifact_id = $1::uuid AND user_id = $2::uuid
        ORDER BY revision DESC
        LIMIT $3 OFFSET $4
        """,
        artifact_id,
        user_id,
        limit,
        offset,
    )
    return [dict(r) for r in rows], int(total)


async def get_version(
    artifact_id: str, version_id: str, user_id: str
) -> Optional[dict]:
    if _mem() is not None:
        row = _mem().versions.get(version_id)
        if (
            row is None
            or row["artifact_id"] != artifact_id
            or row["user_id"] != user_id
        ):
            return None
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        SELECT id, artifact_id, user_id, revision, title, content, metadata, created_at
        FROM artifact_versions
        WHERE id = $1::uuid AND artifact_id = $2::uuid AND user_id = $3::uuid
        """,
        version_id,
        artifact_id,
        user_id,
    )
    return dict(row) if row else None
