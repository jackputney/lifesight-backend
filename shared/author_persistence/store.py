"""Author persistence storage — Postgres via shared.db.pool, optional memory for tests.

Ownership: every query scopes by user_id derived from the JWT at the router.
Request bodies never supply ownership fields.

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

from shared import db

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100


class ConflictError(Exception):
    """Optimistic concurrency failure (stale expected_revision)."""

    def __init__(self, current: dict):
        self.current = current
        super().__init__("Document revision conflict")


def normalized_uuid(value: Any) -> Optional[str]:
    """Canonical UUID string, or None when the value is not a UUID at all.

    Path parameters arrive as arbitrary strings. Without this guard a malformed
    id reaches a `$1::uuid` bind, asyncpg raises DataError, and the request 500s
    instead of taking the ordinary 404 path. Deliberately duplicated in
    shared/author_pipeline/store.py rather than shared between the two author
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


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def serialize_project(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "description": row.get("description"),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def serialize_document(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "title": row["title"],
        "content": row["content"],
        "revision": int(row["revision"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def serialize_version(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "document_id": str(row["document_id"]),
        "revision": int(row["revision"]),
        "title": row["title"],
        "content": row["content"],
        "created_at": _iso(row["created_at"]),
    }


# ---------------------------------------------------------------------------
# In-memory store (tests)
# ---------------------------------------------------------------------------

@dataclass
class _MemoryStore:
    projects: dict[str, dict] = field(default_factory=dict)
    documents: dict[str, dict] = field(default_factory=dict)
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


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

async def create_project(user_id: str, title: str, description: Optional[str] = None) -> dict:
    title = title.strip()
    if _mem() is not None:
        now = _now()
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "title": title,
            "description": description,
            "created_at": now,
            "updated_at": now,
        }
        _mem().projects[row["id"]] = row
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        INSERT INTO author_projects (user_id, title, description)
        VALUES ($1::uuid, $2, $3)
        RETURNING id, user_id, title, description, created_at, updated_at
        """,
        user_id, title, description,
    )
    return dict(row)


async def get_project(project_id: str, user_id: str) -> Optional[dict]:
    project_id = normalized_uuid(project_id)
    if project_id is None:
        return None

    if _mem() is not None:
        row = _mem().projects.get(project_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, title, description, created_at, updated_at
        FROM author_projects
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        project_id, user_id,
    )
    return dict(row) if row else None


async def list_projects(user_id: str, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0) -> tuple[list[dict], int]:
    limit, offset = clamp_pagination(limit, offset)
    if _mem() is not None:
        rows = [deepcopy(p) for p in _mem().projects.values() if p["user_id"] == user_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[offset: offset + limit], len(rows)

    total = await db.pool().fetchval(
        "SELECT COUNT(*) FROM author_projects WHERE user_id = $1::uuid",
        user_id,
    )
    rows = await db.pool().fetch(
        """
        SELECT id, user_id, title, description, created_at, updated_at
        FROM author_projects
        WHERE user_id = $1::uuid
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        user_id, limit, offset,
    )
    return [dict(r) for r in rows], int(total)


async def update_project(
    project_id: str,
    user_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[dict]:
    if title is None and description is None:
        return await get_project(project_id, user_id)
    project_id = normalized_uuid(project_id)
    if project_id is None:
        return None

    if _mem() is not None:
        row = _mem().projects.get(project_id)
        if row is None or row["user_id"] != user_id:
            return None
        if title is not None:
            row["title"] = title.strip()
        if description is not None:
            row["description"] = description
        row["updated_at"] = _now()
        return deepcopy(row)

    sets: list[str] = []
    args: list[Any] = [project_id, user_id]
    if title is not None:
        args.append(title.strip())
        sets.append(f"title = ${len(args)}")
    if description is not None:
        args.append(description)
        sets.append(f"description = ${len(args)}")
    sets.append("updated_at = now()")
    sql = f"""
        UPDATE author_projects
        SET {', '.join(sets)}
        WHERE id = $1::uuid AND user_id = $2::uuid
        RETURNING id, user_id, title, description, created_at, updated_at
    """
    row = await db.pool().fetchrow(sql, *args)
    return dict(row) if row else None


async def delete_project(project_id: str, user_id: str) -> bool:
    """Delete project; documents and versions CASCADE (DB) or are removed in memory."""
    project_id = normalized_uuid(project_id)
    if project_id is None:
        return False

    if _mem() is not None:
        row = _mem().projects.get(project_id)
        if row is None or row["user_id"] != user_id:
            return False
        doc_ids = [
            d["id"] for d in _mem().documents.values()
            if d["project_id"] == project_id and d["user_id"] == user_id
        ]
        for did in doc_ids:
            for vid in [v["id"] for v in _mem().versions.values() if v["document_id"] == did]:
                del _mem().versions[vid]
            del _mem().documents[did]
        del _mem().projects[project_id]
        return True

    result = await db.pool().execute(
        "DELETE FROM author_projects WHERE id = $1::uuid AND user_id = $2::uuid",
        project_id, user_id,
    )
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

async def _insert_version_row(
    document_id: str,
    user_id: str,
    revision: int,
    title: str,
    content: str,
    *,
    created_at: Optional[datetime] = None,
) -> dict:
    if _mem() is not None:
        now = created_at or _now()
        row = {
            "id": str(uuid.uuid4()),
            "document_id": document_id,
            "user_id": user_id,
            "revision": revision,
            "title": title,
            "content": content,
            "created_at": now,
        }
        _mem().versions[row["id"]] = row
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        INSERT INTO author_document_versions (document_id, user_id, revision, title, content)
        VALUES ($1::uuid, $2::uuid, $3, $4, $5)
        RETURNING id, document_id, user_id, revision, title, content, created_at
        """,
        document_id, user_id, revision, title, content,
    )
    return dict(row)


async def create_document(
    project_id: str,
    user_id: str,
    title: str,
    content: str = "",
) -> Optional[dict]:
    """Create document under an owned project; seeds revision 1 snapshot. None if project missing."""
    project = await get_project(project_id, user_id)
    if project is None:
        return None
    project_id = str(project["id"])
    title = title.strip()
    content = content if content is not None else ""

    if _mem() is not None:
        now = _now()
        row = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "user_id": user_id,
            "title": title,
            "content": content,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        _mem().documents[row["id"]] = row
        await _insert_version_row(row["id"], user_id, 1, title, content, created_at=now)
        return deepcopy(row)

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO author_documents (project_id, user_id, title, content, revision)
                VALUES ($1::uuid, $2::uuid, $3, $4, 1)
                RETURNING id, project_id, user_id, title, content, revision, created_at, updated_at
                """,
                project_id, user_id, title, content,
            )
            await conn.execute(
                """
                INSERT INTO author_document_versions (document_id, user_id, revision, title, content)
                VALUES ($1::uuid, $2::uuid, 1, $3, $4)
                """,
                row["id"], user_id, title, content,
            )
            return dict(row)


async def get_document(document_id: str, user_id: str) -> Optional[dict]:
    document_id = normalized_uuid(document_id)
    if document_id is None:
        return None

    if _mem() is not None:
        row = _mem().documents.get(document_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        SELECT id, project_id, user_id, title, content, revision, created_at, updated_at
        FROM author_documents
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        document_id, user_id,
    )
    return dict(row) if row else None


async def list_documents(
    project_id: str,
    user_id: str,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> Optional[tuple[list[dict], int]]:
    """None if project not owned; else (page, total)."""
    project = await get_project(project_id, user_id)
    if project is None:
        return None
    project_id = str(project["id"])
    limit, offset = clamp_pagination(limit, offset)

    if _mem() is not None:
        rows = [
            deepcopy(d) for d in _mem().documents.values()
            if d["project_id"] == project_id and d["user_id"] == user_id
        ]
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        return rows[offset: offset + limit], len(rows)

    total = await db.pool().fetchval(
        """
        SELECT COUNT(*) FROM author_documents
        WHERE project_id = $1::uuid AND user_id = $2::uuid
        """,
        project_id, user_id,
    )
    rows = await db.pool().fetch(
        """
        SELECT id, project_id, user_id, title, content, revision, created_at, updated_at
        FROM author_documents
        WHERE project_id = $1::uuid AND user_id = $2::uuid
        ORDER BY updated_at DESC
        LIMIT $3 OFFSET $4
        """,
        project_id, user_id, limit, offset,
    )
    return [dict(r) for r in rows], int(total)


async def update_document(
    document_id: str,
    user_id: str,
    *,
    expected_revision: int,
    title: Optional[str] = None,
    content: Optional[str] = None,
) -> Optional[dict]:
    """Autosave-safe update. Raises ConflictError on revision mismatch. None if not found."""
    current = await get_document(document_id, user_id)
    if current is None:
        return None
    document_id = str(current["id"])
    if int(current["revision"]) != int(expected_revision):
        raise ConflictError(current)

    new_title = current["title"] if title is None else title.strip()
    new_content = current["content"] if content is None else content
    new_revision = int(current["revision"]) + 1

    if _mem() is not None:
        row = _mem().documents[document_id]
        row["title"] = new_title
        row["content"] = new_content
        row["revision"] = new_revision
        row["updated_at"] = _now()
        await _insert_version_row(document_id, user_id, new_revision, new_title, new_content)
        return deepcopy(row)

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE author_documents
                SET title = $3, content = $4, revision = $5, updated_at = now()
                WHERE id = $1::uuid AND user_id = $2::uuid AND revision = $6
                RETURNING id, project_id, user_id, title, content, revision, created_at, updated_at
                """,
                document_id, user_id, new_title, new_content, new_revision, expected_revision,
            )
            if row is None:
                # Race: another writer won between read and update.
                fresh = await conn.fetchrow(
                    """
                    SELECT id, project_id, user_id, title, content, revision, created_at, updated_at
                    FROM author_documents
                    WHERE id = $1::uuid AND user_id = $2::uuid
                    """,
                    document_id, user_id,
                )
                if fresh is None:
                    return None
                raise ConflictError(dict(fresh))
            await conn.execute(
                """
                INSERT INTO author_document_versions (document_id, user_id, revision, title, content)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5)
                """,
                document_id, user_id, new_revision, new_title, new_content,
            )
            return dict(row)


async def delete_document(document_id: str, user_id: str) -> bool:
    document_id = normalized_uuid(document_id)
    if document_id is None:
        return False

    if _mem() is not None:
        row = _mem().documents.get(document_id)
        if row is None or row["user_id"] != user_id:
            return False
        for vid in [v["id"] for v in _mem().versions.values() if v["document_id"] == document_id]:
            del _mem().versions[vid]
        del _mem().documents[document_id]
        return True

    result = await db.pool().execute(
        "DELETE FROM author_documents WHERE id = $1::uuid AND user_id = $2::uuid",
        document_id, user_id,
    )
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# Versions (append-only)
# ---------------------------------------------------------------------------

async def create_version_snapshot(document_id: str, user_id: str) -> Optional[dict]:
    """Checkpoint current head as a new revision without requiring content change."""
    current = await get_document(document_id, user_id)
    if current is None:
        return None
    document_id = str(current["id"])
    expected = int(current["revision"])
    new_revision = expected + 1

    if _mem() is not None:
        row = _mem().documents[document_id]
        row["revision"] = new_revision
        row["updated_at"] = _now()
        return await _insert_version_row(
            document_id, user_id, new_revision, row["title"], row["content"]
        )

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            doc = await conn.fetchrow(
                """
                UPDATE author_documents
                SET revision = $3, updated_at = now()
                WHERE id = $1::uuid AND user_id = $2::uuid AND revision = $4
                RETURNING id, title, content, revision
                """,
                document_id, user_id, new_revision, expected,
            )
            if doc is None:
                fresh = await get_document(document_id, user_id)
                if fresh is None:
                    return None
                raise ConflictError(fresh)
            ver = await conn.fetchrow(
                """
                INSERT INTO author_document_versions (document_id, user_id, revision, title, content)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5)
                RETURNING id, document_id, user_id, revision, title, content, created_at
                """,
                document_id, user_id, new_revision, doc["title"], doc["content"],
            )
            return dict(ver)


async def list_versions(
    document_id: str,
    user_id: str,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> Optional[tuple[list[dict], int]]:
    document = await get_document(document_id, user_id)
    if document is None:
        return None
    document_id = str(document["id"])
    limit, offset = clamp_pagination(limit, offset)

    if _mem() is not None:
        rows = [
            deepcopy(v) for v in _mem().versions.values()
            if v["document_id"] == document_id and v["user_id"] == user_id
        ]
        rows.sort(key=lambda r: r["revision"], reverse=True)
        return rows[offset: offset + limit], len(rows)

    total = await db.pool().fetchval(
        """
        SELECT COUNT(*) FROM author_document_versions
        WHERE document_id = $1::uuid AND user_id = $2::uuid
        """,
        document_id, user_id,
    )
    rows = await db.pool().fetch(
        """
        SELECT id, document_id, user_id, revision, title, content, created_at
        FROM author_document_versions
        WHERE document_id = $1::uuid AND user_id = $2::uuid
        ORDER BY revision DESC
        LIMIT $3 OFFSET $4
        """,
        document_id, user_id, limit, offset,
    )
    return [dict(r) for r in rows], int(total)


async def get_version(document_id: str, version_id: str, user_id: str) -> Optional[dict]:
    document_id = normalized_uuid(document_id)
    version_id = normalized_uuid(version_id)
    if document_id is None or version_id is None:
        return None

    if _mem() is not None:
        row = _mem().versions.get(version_id)
        if (
            row is None
            or row["document_id"] != document_id
            or row["user_id"] != user_id
        ):
            return None
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        SELECT id, document_id, user_id, revision, title, content, created_at
        FROM author_document_versions
        WHERE id = $1::uuid AND document_id = $2::uuid AND user_id = $3::uuid
        """,
        version_id, document_id, user_id,
    )
    return dict(row) if row else None
