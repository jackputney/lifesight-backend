"""Author capture → refine → flag → review — REST contract (backend-first).

Identity always via Depends(get_current_user_id). Ownership is never accepted
from the request body. Cross-user or missing resources → 404 (anti-oracle,
consistent with author_persistence). Deciding an already-resolved flag → 409.

Raw captures are append-only: this router deliberately exposes no route that
edits or deletes a capture, and migration 017 blocks it at the database too.
Refinement and flag decisions create NEW draft versions instead.

Coexists with routers/author_persistence.py (projects/documents); neither
surface reads or writes the other's tables.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from shared.auth import get_current_user_id
from shared.author_pipeline import service, store

router = APIRouter(tags=["author-pipeline"])


class SessionCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    conversation_id: Optional[str] = None
    manuscript_id: Optional[str] = None


class CaptureCreate(BaseModel):
    source: Literal["voice", "typed"]
    raw_text: str = Field(..., min_length=1)


class RefineRequest(BaseModel):
    refinement_level: Optional[
        Literal["light_cleanup", "preserve_voice", "polish", "structural_rewrite"]
    ] = None
    capture_from: Optional[int] = Field(default=None, ge=0)
    capture_to: Optional[int] = Field(default=None, ge=0)


class FlagDecisionRequest(BaseModel):
    decision: Literal["accept", "reject", "edit", "defer"]
    replacement_text: Optional[str] = None


def _page(items: list, total: int, limit: int, offset: int) -> dict:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@router.post("/author/sessions")
async def create_session(
    body: Optional[SessionCreate] = None,
    user_id: str = Depends(get_current_user_id),
):
    """Every field is optional, so a bodyless start-dictating call is valid."""
    payload = body or SessionCreate()
    row = await store.create_session(
        user_id,
        title=payload.title,
        conversation_id=payload.conversation_id,
        manuscript_id=payload.manuscript_id,
    )
    return store.serialize_session(row)


@router.get("/author/sessions")
async def list_sessions(
    limit: int = Query(default=store.DEFAULT_PAGE_LIMIT, ge=1, le=store.MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
):
    rows, total = await store.list_sessions(user_id, limit=limit, offset=offset)
    return _page([store.serialize_session(r) for r in rows], total, limit, offset)


@router.get("/author/sessions/{session_id}")
async def get_session(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Both views in one call: raw `captures` and derived `draft_versions`."""
    return await service.session_detail(session_id, user_id)


@router.post("/author/sessions/{session_id}/end")
async def end_session(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    row = await store.end_session(session_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail=service.SESSION_NOT_FOUND)
    return store.serialize_session(row)


# ---------------------------------------------------------------------------
# Captures — append and read only (no PATCH/PUT/DELETE by design)
# ---------------------------------------------------------------------------

@router.get("/author/sessions/{session_id}/captures")
async def list_captures(
    session_id: str,
    limit: int = Query(default=store.DEFAULT_PAGE_LIMIT, ge=1, le=store.MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
):
    """Provenance view — exactly what the author said, never the refined text."""
    result = await store.list_captures(session_id, user_id, limit=limit, offset=offset)
    if result is None:
        raise HTTPException(status_code=404, detail=service.SESSION_NOT_FOUND)
    rows, total = result
    return _page([store.serialize_capture(r) for r in rows], total, limit, offset)


@router.post("/author/sessions/{session_id}/captures")
async def append_capture(
    session_id: str,
    body: CaptureCreate,
    user_id: str = Depends(get_current_user_id),
):
    """Append raw dictation. The server assigns the next sequence."""
    return await service.append_capture(
        session_id, user_id, source=body.source, raw_text=body.raw_text
    )


# ---------------------------------------------------------------------------
# Refinement + review
# ---------------------------------------------------------------------------

@router.post("/author/sessions/{session_id}/refine")
async def refine_session(
    session_id: str,
    body: Optional[RefineRequest] = None,
    user_id: str = Depends(get_current_user_id),
):
    """Create a new derived draft version plus advisory flags.

    A missing body means "refine this whole session with preserve_voice".
    """
    payload = body or RefineRequest()
    return await service.refine_session(
        session_id,
        user_id,
        refinement_level=payload.refinement_level,
        capture_from=payload.capture_from,
        capture_to=payload.capture_to,
    )


@router.post("/author/flags/{flag_id}/decision")
async def decide_flag(
    flag_id: str,
    body: FlagDecisionRequest,
    user_id: str = Depends(get_current_user_id),
):
    """accept/edit create a new draft version; reject/defer change no text."""
    return await service.decide_flag(
        flag_id,
        user_id,
        decision=body.decision,
        replacement_text=body.replacement_text,
    )
