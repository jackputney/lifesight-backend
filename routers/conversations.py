"""Authenticated conversation history APIs (JWT ownership only)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from shared import db
from shared.auth import get_current_user_id
from shared.conversation_titles import fallback_title

router = APIRouter(tags=["conversations"])


class ConversationOut(BaseModel):
    id: str
    mode: str
    title: str
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_message_at: Optional[datetime] = None


class ConversationListOut(BaseModel):
    conversations: list[ConversationOut]
    next_cursor: Optional[str] = None


class MessageOut(BaseModel):
    seq: int
    role: str
    content: Any
    created_at: Optional[datetime] = None


class MessageListOut(BaseModel):
    conversation_id: str
    mode: str
    messages: list[MessageOut]


def _display_title(row: dict) -> str:
    title = (row.get("title") or "").strip()
    if title:
        return title
    return fallback_title(str(row.get("mode") or "fitness"))


def _to_out(row: dict) -> ConversationOut:
    updated = row.get("last_message_at") or row.get("created_at")
    return ConversationOut(
        id=str(row["id"]),
        mode=str(row["mode"]),
        title=_display_title(row),
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        updated_at=updated,
        last_message_at=row.get("last_message_at"),
    )


async def _owned_conversation(conversation_id: str, user_id: str) -> dict:
    try:
        UUID(conversation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="conversation_id must be a UUID") from exc
    convo = await db.get_conversation(conversation_id)
    if convo is None or str(convo["user_id"]) != user_id:
        raise HTTPException(status_code=404, detail="conversation not found")
    return convo


@router.get("/conversations", response_model=ConversationListOut)
async def list_conversations(
    limit: int = Query(20, ge=1, le=50),
    cursor: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user_id),
):
    try:
        rows = await db.list_conversations(user_id, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    items = [_to_out(r) for r in rows]
    next_cursor = None
    if len(rows) == limit and rows:
        last = rows[-1]
        ts = last.get("last_message_at") or last.get("created_at")
        if ts is not None:
            next_cursor = f"{ts.isoformat()}|{last['id']}"
    return ConversationListOut(conversations=items, next_cursor=next_cursor)


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: str,
    user_id: str = Depends(get_current_user_id),
):
    convo = await _owned_conversation(conversation_id, user_id)
    return _to_out(convo)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=MessageListOut,
)
async def get_conversation_messages(
    conversation_id: str,
    limit: int = Query(50, ge=1, le=100),
    before_seq: Optional[int] = Query(None, ge=0),
    user_id: str = Depends(get_current_user_id),
):
    convo = await _owned_conversation(conversation_id, user_id)
    rows = await db.list_messages_page(
        conversation_id, limit=limit, before_seq=before_seq
    )
    messages = [
        MessageOut(
            seq=int(r["seq"]),
            role=str(r["role"]),
            content=r["content"],
            created_at=r.get("created_at"),
        )
        for r in rows
    ]
    return MessageListOut(
        conversation_id=str(convo["id"]),
        mode=str(convo["mode"]),
        messages=messages,
    )
