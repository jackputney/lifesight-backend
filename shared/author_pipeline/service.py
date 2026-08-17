"""Author pipeline orchestration — capture → refine → flag → review.

These flows raise HTTPException directly so the router stays a thin transport
layer for them. Simple session CRUD goes straight to `store` from the router,
following the author_persistence template.

Invariant enforced here: no flow reads a capture for anything except input.
Refinement and flag decisions only ever insert new author_draft_versions rows.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from shared.author_pipeline import refine as refine_module
from shared.author_pipeline import store

SESSION_NOT_FOUND = "Session not found"
FLAG_NOT_FOUND = "Flag not found"
NOT_LOCALIZED = (
    "Flag is not localized to a span, so no text change can be applied. "
    "Reject or defer it, or refine again."
)
SEQUENCE_CONTENTION = (
    "Too many captures are being appended to this session at once. "
    "Nothing was stored — send this chunk again."
)


async def create_session(
    user_id: str,
    *,
    title: Optional[str] = None,
    conversation_id: Optional[str] = None,
    manuscript_id: Optional[str] = None,
) -> dict:
    """Start a capture session, validating both soft references first.

    `conversation_id` and `manuscript_id` carry no foreign key, so without this
    check a caller could file a session against another user's conversation or
    manuscript and hand the first feature that dereferences either column a
    ready-made IDOR.
    """
    if conversation_id is not None:
        if store.normalized_uuid(conversation_id) is None:
            raise HTTPException(status_code=400, detail="conversation_id must be a UUID")
        if not await store.conversation_is_owned(conversation_id, user_id):
            raise HTTPException(status_code=404, detail="Conversation not found")

    if manuscript_id is not None:
        if store.normalized_uuid(manuscript_id) is None:
            raise HTTPException(status_code=400, detail="manuscript_id must be a UUID")
        if not await store.manuscript_is_owned(manuscript_id, user_id):
            raise HTTPException(status_code=404, detail="Manuscript not found")

    row = await store.create_session(
        user_id,
        title=title,
        conversation_id=conversation_id,
        manuscript_id=manuscript_id,
    )
    return store.serialize_session(row)


async def session_detail(session_id: str, user_id: str) -> dict:
    """Session with BOTH views: raw captures and derived draft versions.

    `captures` is what the author actually said; `draft_versions` is what
    LifeSight refined it into. They are separate keys and never merged.
    """
    session = await store.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail=SESSION_NOT_FOUND)

    captures = await store.all_captures(session_id, user_id)
    versions = await store.list_draft_versions(session_id, user_id)
    open_flags = await store.list_flags(session_id, user_id, status="open")
    return {
        "session": store.serialize_session(session),
        "captures": [store.serialize_capture(c) for c in captures],
        "draft_versions": [store.serialize_draft_version(v) for v in versions],
        "open_flags": [store.serialize_flag(f) for f in open_flags],
    }


async def append_capture(
    session_id: str,
    user_id: str,
    *,
    source: str,
    raw_text: str,
) -> dict:
    """Append immutable raw dictation. Ended sessions accept no new captures."""
    session = await store.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail=SESSION_NOT_FOUND)
    if session["status"] == "ended":
        raise HTTPException(
            status_code=409,
            detail="Session has ended and cannot accept new captures",
        )
    if source not in store.CAPTURE_SOURCES:
        raise HTTPException(status_code=400, detail="Unsupported capture source")
    if not raw_text.strip():
        raise HTTPException(status_code=400, detail="raw_text cannot be empty")
    if len(raw_text) > store.MAX_CAPTURE_CHARS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"raw_text is limited to {store.MAX_CAPTURE_CHARS} characters. "
                "Send dictation in chunks."
            ),
        )

    try:
        row = await store.append_capture(
            session_id, user_id, source=source, raw_text=raw_text
        )
    except store.CaptureSequenceContention as exc:
        raise HTTPException(status_code=503, detail=SEQUENCE_CONTENTION) from exc
    if row is None:
        raise HTTPException(status_code=404, detail=SESSION_NOT_FOUND)
    return store.serialize_capture(row)


async def refine_session(
    session_id: str,
    user_id: str,
    *,
    refinement_level: Optional[str] = None,
    capture_from: Optional[int] = None,
    capture_to: Optional[int] = None,
) -> dict:
    """Create a NEW draft version (and its flags) from a capture range.

    Captures and prior versions are never modified. An omitted or null
    refinement_level means `preserve_voice`.
    """
    session = await store.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status_code=404, detail=SESSION_NOT_FOUND)

    level = refinement_level or store.DEFAULT_REFINEMENT_LEVEL
    if level not in store.REFINEMENT_LEVELS:
        raise HTTPException(status_code=400, detail="Unsupported refinement_level")

    captures = await store.all_captures(session_id, user_id)
    if not captures:
        raise HTTPException(status_code=400, detail="Session has no captures to refine")

    sequences = [int(c["sequence"]) for c in captures]
    low = sequences[0] if capture_from is None else int(capture_from)
    high = sequences[-1] if capture_to is None else int(capture_to)
    if low > high:
        raise HTTPException(
            status_code=400,
            detail="capture_from must be less than or equal to capture_to",
        )

    selected = await store.captures_in_range(session_id, user_id, low, high)
    if not selected:
        raise HTTPException(
            status_code=400, detail="No captures in the requested range"
        )

    result = await refine_module.refine_captures(selected, level)
    version, flags = await store.create_refinement(
        session_id,
        user_id,
        refinement_level=level,
        content=result["content"],
        source_capture_from=int(selected[0]["sequence"]),
        source_capture_to=int(selected[-1]["sequence"]),
        model_identifier=result["model_identifier"],
        flags=result["flags"],
    )
    return {
        "draft_version": store.serialize_draft_version(version),
        "flags": [store.serialize_flag(f) for f in flags],
    }


def _replace_span(content: str, span_start: Optional[int], span_end: Optional[int], text: str) -> str:
    if (
        span_start is None
        or span_end is None
        or not (0 <= int(span_start) <= int(span_end) <= len(content))
    ):
        raise HTTPException(status_code=400, detail=NOT_LOCALIZED)
    return content[: int(span_start)] + text + content[int(span_end):]


async def decide_flag(
    flag_id: str,
    user_id: str,
    *,
    decision: str,
    replacement_text: Optional[str] = None,
) -> dict:
    """Resolve one flag.

    accept/edit insert a new draft version derived from the flagged one;
    reject/defer only record the decision. Nothing here can touch a capture.
    """
    if decision not in store.DECISIONS:
        raise HTTPException(status_code=400, detail="Unsupported decision")

    flag = await store.get_flag(flag_id, user_id)
    if flag is None:
        raise HTTPException(status_code=404, detail=FLAG_NOT_FOUND)
    if flag["status"] != "open":
        raise HTTPException(
            status_code=409,
            detail=f"Flag already resolved as '{flag['status']}'",
        )

    source_version = await store.get_draft_version(str(flag["draft_version_id"]), user_id)
    if source_version is None:
        raise HTTPException(status_code=404, detail=FLAG_NOT_FOUND)

    new_content: Optional[str] = None
    if decision == "accept":
        suggested = flag.get("suggested_change")
        if suggested is None:
            raise HTTPException(
                status_code=400,
                detail="Flag has no suggested change to accept",
            )
        new_content = _replace_span(
            source_version["content"], flag.get("span_start"), flag.get("span_end"), suggested
        )
    elif decision == "edit":
        if replacement_text is None:
            raise HTTPException(
                status_code=400,
                detail="replacement_text is required for an edit decision",
            )
        new_content = _replace_span(
            source_version["content"],
            flag.get("span_start"),
            flag.get("span_end"),
            replacement_text,
        )

    stored_flag, stored_decision, version = await store.record_flag_decision(
        flag,
        user_id,
        decision=decision,
        replacement_text=replacement_text,
        source_version=source_version,
        new_content=new_content,
    )
    return {
        "flag": store.serialize_flag(stored_flag),
        "decision": store.serialize_decision(stored_decision),
        "draft_version": store.serialize_draft_version(version) if version else None,
    }
