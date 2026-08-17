"""Author projects / documents / versions — REST contract (backend-first).

Identity always via Depends(get_current_user_id). Ownership is never accepted
from the request body. Cross-user, missing, AND malformed path ids all → 404
(anti-oracle, consistent with manuscripts). Stale autosave → 409.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from shared.auth import get_current_user_id
from shared.author_persistence import store

router = APIRouter(tags=["author-persistence"])


class ProjectCreate(BaseModel):
    title: str = Field(..., min_length=1)
    description: Optional[str] = None


class ProjectUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None


class DocumentCreate(BaseModel):
    title: str = Field(..., min_length=1)
    content: str = ""


class DocumentUpdate(BaseModel):
    expected_revision: int = Field(..., ge=1)
    title: Optional[str] = Field(default=None, min_length=1)
    content: Optional[str] = None


def _page(items: list, total: int, limit: int, offset: int) -> dict:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@router.post("/author/projects")
async def create_project(
    body: ProjectCreate,
    user_id: str = Depends(get_current_user_id),
):
    row = await store.create_project(user_id, body.title, body.description)
    return store.serialize_project(row)


@router.get("/author/projects")
async def list_projects(
    limit: int = Query(default=store.DEFAULT_PAGE_LIMIT, ge=1, le=store.MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
):
    rows, total = await store.list_projects(user_id, limit=limit, offset=offset)
    return _page([store.serialize_project(r) for r in rows], total, limit, offset)


@router.get("/author/projects/{project_id}")
async def get_project(
    project_id: str,
    user_id: str = Depends(get_current_user_id),
):
    row = await store.get_project(project_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return store.serialize_project(row)


@router.patch("/author/projects/{project_id}")
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    user_id: str = Depends(get_current_user_id),
):
    if body.title is None and body.description is None:
        raise HTTPException(status_code=400, detail="No fields to update")
    row = await store.update_project(
        project_id, user_id, title=body.title, description=body.description
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return store.serialize_project(row)


@router.delete("/author/projects/{project_id}")
async def delete_project(
    project_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Hard-delete project; documents and versions cascade."""
    ok = await store.delete_project(project_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"deleted": True, "id": project_id}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@router.post("/author/projects/{project_id}/documents")
async def create_document(
    project_id: str,
    body: DocumentCreate,
    user_id: str = Depends(get_current_user_id),
):
    row = await store.create_document(project_id, user_id, body.title, body.content)
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return store.serialize_document(row)


@router.get("/author/projects/{project_id}/documents")
async def list_documents(
    project_id: str,
    limit: int = Query(default=store.DEFAULT_PAGE_LIMIT, ge=1, le=store.MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
):
    result = await store.list_documents(project_id, user_id, limit=limit, offset=offset)
    if result is None:
        raise HTTPException(status_code=404, detail="Project not found")
    rows, total = result
    return _page([store.serialize_document(r) for r in rows], total, limit, offset)


@router.get("/author/documents/{document_id}")
async def get_document(
    document_id: str,
    user_id: str = Depends(get_current_user_id),
):
    row = await store.get_document(document_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return store.serialize_document(row)


@router.patch("/author/documents/{document_id}")
async def update_document(
    document_id: str,
    body: DocumentUpdate,
    user_id: str = Depends(get_current_user_id),
):
    if body.title is None and body.content is None:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        row = await store.update_document(
            document_id,
            user_id,
            expected_revision=body.expected_revision,
            title=body.title,
            content=body.content,
        )
    except store.ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Document revision conflict",
                "current": store.serialize_document(exc.current),
            },
        ) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return store.serialize_document(row)


@router.delete("/author/documents/{document_id}")
async def delete_document(
    document_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Hard-delete document; version history cascades."""
    ok = await store.delete_document(document_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"deleted": True, "id": document_id}


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

@router.post("/author/documents/{document_id}/versions")
async def create_version(
    document_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Append an immutable checkpoint of the current document head."""
    try:
        row = await store.create_version_snapshot(document_id, user_id)
    except store.ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Document revision conflict",
                "current": store.serialize_document(exc.current),
            },
        ) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return store.serialize_version(row)


@router.get("/author/documents/{document_id}/versions")
async def list_versions(
    document_id: str,
    limit: int = Query(default=store.DEFAULT_PAGE_LIMIT, ge=1, le=store.MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
):
    result = await store.list_versions(document_id, user_id, limit=limit, offset=offset)
    if result is None:
        raise HTTPException(status_code=404, detail="Document not found")
    rows, total = result
    return _page([store.serialize_version(r) for r in rows], total, limit, offset)


@router.get("/author/documents/{document_id}/versions/{version_id}")
async def get_version(
    document_id: str,
    version_id: str,
    user_id: str = Depends(get_current_user_id),
):
    row = await store.get_version(document_id, version_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return store.serialize_version(row)
