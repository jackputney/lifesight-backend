"""Shared artifacts / versions — REST contract (backend-first).

Identity always via Depends(get_current_user_id). Ownership is never accepted
from the request body. Cross-user or missing resources → 404 (anti-oracle,
consistent with Author). Stale expected_revision → 409 with current artifact.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from shared.artifacts import store
from shared.auth import get_current_user_id

router = APIRouter(tags=["artifacts"])


class ArtifactCreate(BaseModel):
    type: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    content: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactUpdate(BaseModel):
    expected_revision: int = Field(..., ge=1)
    title: Optional[str] = Field(default=None, min_length=1)
    content: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


def _page(items: list, total: int, limit: int, offset: int) -> dict:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/artifacts")
async def create_artifact(
    body: ArtifactCreate,
    user_id: str = Depends(get_current_user_id),
):
    row = await store.create_artifact(
        user_id,
        type=body.type,
        title=body.title,
        content=body.content,
        metadata=body.metadata,
    )
    return store.serialize_artifact(row)


@router.get("/artifacts")
async def list_artifacts(
    limit: int = Query(default=store.DEFAULT_PAGE_LIMIT, ge=1, le=store.MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    type: Optional[str] = Query(default=None, min_length=1),
    user_id: str = Depends(get_current_user_id),
):
    rows, total = await store.list_artifacts(
        user_id, type=type, limit=limit, offset=offset
    )
    return _page([store.serialize_artifact(r) for r in rows], total, limit, offset)


@router.get("/artifacts/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    user_id: str = Depends(get_current_user_id),
):
    row = await store.get_artifact(artifact_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return store.serialize_artifact(row)


@router.patch("/artifacts/{artifact_id}")
async def update_artifact(
    artifact_id: str,
    body: ArtifactUpdate,
    user_id: str = Depends(get_current_user_id),
):
    if body.title is None and body.content is None and body.metadata is None:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        row = await store.update_artifact(
            artifact_id,
            user_id,
            expected_revision=body.expected_revision,
            title=body.title,
            content=body.content,
            metadata=body.metadata,
        )
    except store.ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Artifact revision conflict",
                "current": store.serialize_artifact(exc.current),
            },
        ) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return store.serialize_artifact(row)


@router.delete("/artifacts/{artifact_id}")
async def delete_artifact(
    artifact_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Hard-delete artifact; version history cascades."""
    ok = await store.delete_artifact(artifact_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return {"deleted": True, "id": artifact_id}


@router.post("/artifacts/{artifact_id}/versions")
async def create_version(
    artifact_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Append an immutable checkpoint of the current head (does not bump revision)."""
    row = await store.create_version_snapshot(artifact_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return store.serialize_version(row)


@router.get("/artifacts/{artifact_id}/versions")
async def list_versions(
    artifact_id: str,
    limit: int = Query(default=store.DEFAULT_PAGE_LIMIT, ge=1, le=store.MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
):
    result = await store.list_versions(artifact_id, user_id, limit=limit, offset=offset)
    if result is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    rows, total = result
    return _page([store.serialize_version(r) for r in rows], total, limit, offset)


@router.get("/artifacts/{artifact_id}/versions/{version_id}")
async def get_version(
    artifact_id: str,
    version_id: str,
    user_id: str = Depends(get_current_user_id),
):
    row = await store.get_version(artifact_id, version_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return store.serialize_version(row)
