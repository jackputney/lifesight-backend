"""Personalization storage — Postgres via shared.db.pool, optional memory for tests.

Ownership: every read and write is scoped by user_id, so one user's summaries or
proposals are never visible to another.

This module writes exactly two tables: `personalization_summaries` (evidence)
and `prompt_change_proposals` (always inserted as status='pending'). It has no
write path to the `user_prompt_overrides` table — approving and applying a
proposal is Oliver admin's job, not the backend's.
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import asyncpg

from shared import db

SUMMARY_SCOPES: tuple[str, ...] = ("daily", "multi_day", "weekly")
PROPOSAL_STATUSES: tuple[str, ...] = ("pending", "approved", "rejected", "applied")

# Same allowlist as user_prompt_overrides.mode / MODE_REGISTRY (NULL = global).
PROPOSAL_MODES: tuple[str, ...] = (
    "fitness",
    "diet",
    "author",
    "brainstorm",
    "mail_calendar",
    "jarvis",
    "checkin",
)


class PendingProposalExistsError(Exception):
    """A pending proposal already targets this (user_id, mode)."""

    def __init__(self, user_id: str, mode: Optional[str]):
        self.user_id = user_id
        self.mode = mode
        super().__init__(
            "A pending prompt change proposal already exists for this "
            "user and mode; resolve it before creating another."
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _ids(values: Any) -> list[str]:
    return [str(v) for v in (values or [])]


def _evidence(value: Any) -> dict:
    """asyncpg returns jsonb as raw text; memory rows hold a dict already."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def validate_scope(scope: str) -> str:
    if scope not in SUMMARY_SCOPES:
        raise ValueError(f"scope must be one of {SUMMARY_SCOPES}, got {scope!r}")
    return scope


def validate_mode(mode: Optional[str]) -> Optional[str]:
    if mode is None:
        return None
    if mode not in PROPOSAL_MODES:
        raise ValueError(f"mode must be NULL or one of {PROPOSAL_MODES}, got {mode!r}")
    return mode


def serialize_summary(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "scope": row["scope"],
        "period_start": _iso(row["period_start"]),
        "period_end": _iso(row["period_end"]),
        "summary": row["summary"],
        "source_conversation_ids": _ids(row.get("source_conversation_ids")),
        "source_summary_ids": _ids(row.get("source_summary_ids")),
        "model_identifier": row["model_identifier"],
        "created_at": _iso(row["created_at"]),
    }


def serialize_proposal(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "mode": row.get("mode"),
        "proposed_instructions": row["proposed_instructions"],
        "final_instructions": row.get("final_instructions"),
        "reasoning": row["reasoning"],
        "evidence": _evidence(row.get("evidence")),
        "risks": row.get("risks"),
        "status": row["status"],
        "model_identifier": row["model_identifier"],
        "created_at": _iso(row["created_at"]),
        "reviewed_at": _iso(row["reviewed_at"]) if row.get("reviewed_at") else None,
        "reviewed_by": row.get("reviewed_by"),
        "applied_override_id": (
            str(row["applied_override_id"]) if row.get("applied_override_id") else None
        ),
    }


# ---------------------------------------------------------------------------
# In-memory store (tests)
# ---------------------------------------------------------------------------

@dataclass
class _MemoryStore:
    summaries: dict[str, dict] = field(default_factory=dict)
    proposals: dict[str, dict] = field(default_factory=dict)
    conversations: dict[str, dict] = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)


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


def memory_seed_conversation(
    user_id: str,
    *,
    mode: str = "fitness",
    conversation_id: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """Memory-store only: register a conversation the summarizer can read."""
    store = _require_memory()
    cid = conversation_id or str(uuid.uuid4())
    store.conversations[cid] = {
        "id": cid,
        "user_id": user_id,
        "mode": mode,
        "title": title,
    }
    return cid


def memory_seed_message(
    conversation_id: str,
    *,
    role: str,
    text: str,
    created_on: date,
    seq: Optional[int] = None,
) -> None:
    """Memory-store only: append one message to a seeded conversation."""
    store = _require_memory()
    if conversation_id not in store.conversations:
        raise RuntimeError(f"unknown seeded conversation {conversation_id}")
    existing = [m for m in store.messages if m["conversation_id"] == conversation_id]
    store.messages.append(
        {
            "conversation_id": conversation_id,
            "seq": len(existing) if seq is None else seq,
            "role": role,
            "content_json": [{"type": "text", "text": text}],
            "created_on": created_on,
        }
    )


# ---------------------------------------------------------------------------
# Raw conversation reads (input for scope=daily)
# ---------------------------------------------------------------------------

async def list_conversations_in_period(
    user_id: str,
    period_start: date,
    period_end: date,
    *,
    limit: int,
) -> list[dict]:
    """Owned conversations with at least one message inside the period.

    Periods are UTC calendar dates: `[period_start 00:00Z, period_end+1 00:00Z)`.
    """
    if _mem() is not None:
        active = {
            m["conversation_id"]
            for m in _mem().messages
            if period_start <= m["created_on"] <= period_end
        }
        rows = [
            {"id": c["id"], "mode": c["mode"], "title": c["title"]}
            for c in _mem().conversations.values()
            if c["user_id"] == user_id and c["id"] in active
        ]
        rows.sort(key=lambda r: r["id"])
        return rows[: max(0, int(limit))]

    rows = await db.pool().fetch(
        """
        SELECT c.id, c.mode, c.title
        FROM conversations c
        WHERE c.user_id = $1::uuid
          AND EXISTS (
              SELECT 1 FROM messages m
              WHERE m.conversation_id = c.id
                AND m.created_at >= ($2::date)::timestamp AT TIME ZONE 'UTC'
                AND m.created_at < ($3::date + 1)::timestamp AT TIME ZONE 'UTC'
          )
        ORDER BY c.created_at ASC
        LIMIT $4
        """,
        user_id, period_start, period_end, int(limit),
    )
    return [dict(r) for r in rows]


async def list_messages_in_period(
    conversation_id: str,
    user_id: str,
    period_start: date,
    period_end: date,
) -> list[dict]:
    """Ordered messages of an owned conversation inside the period."""
    if _mem() is not None:
        convo = _mem().conversations.get(conversation_id)
        if convo is None or convo["user_id"] != user_id:
            return []
        rows = [
            {"seq": m["seq"], "role": m["role"], "content_json": m["content_json"]}
            for m in _mem().messages
            if m["conversation_id"] == conversation_id
            and period_start <= m["created_on"] <= period_end
        ]
        rows.sort(key=lambda r: r["seq"])
        return deepcopy(rows)

    rows = await db.pool().fetch(
        """
        SELECT m.seq, m.role, m.content_json
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.conversation_id = $1::uuid
          AND c.user_id = $2::uuid
          AND m.created_at >= ($3::date)::timestamp AT TIME ZONE 'UTC'
          AND m.created_at < ($4::date + 1)::timestamp AT TIME ZONE 'UTC'
        ORDER BY m.seq ASC
        """,
        conversation_id, user_id, period_start, period_end,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# personalization_summaries
# ---------------------------------------------------------------------------

async def upsert_summary(
    user_id: str,
    *,
    scope: str,
    period_start: date,
    period_end: date,
    summary: str,
    source_conversation_ids: list[str],
    source_summary_ids: list[str],
    model_identifier: str,
) -> dict:
    """Idempotent per (user_id, scope, period_start, period_end).

    Re-running the same period overwrites the summary text, evidence, and model
    in place instead of accumulating duplicate rows.
    """
    validate_scope(scope)
    if period_end < period_start:
        raise ValueError("period_end must be on or after period_start")
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("summary must be non-empty")
    convo_ids = _ids(source_conversation_ids)
    summary_ids = _ids(source_summary_ids)

    if _mem() is not None:
        key = (user_id, scope, period_start, period_end)
        for row in _mem().summaries.values():
            if (
                row["user_id"],
                row["scope"],
                row["period_start"],
                row["period_end"],
            ) == key:
                row["summary"] = summary
                row["source_conversation_ids"] = list(convo_ids)
                row["source_summary_ids"] = list(summary_ids)
                row["model_identifier"] = model_identifier
                return deepcopy(row)
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "scope": scope,
            "period_start": period_start,
            "period_end": period_end,
            "summary": summary,
            "source_conversation_ids": list(convo_ids),
            "source_summary_ids": list(summary_ids),
            "model_identifier": model_identifier,
            "created_at": _now(),
        }
        _mem().summaries[row["id"]] = row
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        INSERT INTO personalization_summaries (
            user_id, scope, period_start, period_end, summary,
            source_conversation_ids, source_summary_ids, model_identifier
        )
        VALUES ($1::uuid, $2, $3::date, $4::date, $5, $6::uuid[], $7::uuid[], $8)
        ON CONFLICT ON CONSTRAINT personalization_summaries_period_uidx
        DO UPDATE SET
            summary = EXCLUDED.summary,
            source_conversation_ids = EXCLUDED.source_conversation_ids,
            source_summary_ids = EXCLUDED.source_summary_ids,
            model_identifier = EXCLUDED.model_identifier
        RETURNING id, user_id, scope, period_start, period_end, summary,
                  source_conversation_ids, source_summary_ids,
                  model_identifier, created_at
        """,
        user_id, scope, period_start, period_end, summary,
        convo_ids, summary_ids, model_identifier,
    )
    return dict(row)


async def get_summary(
    user_id: str,
    *,
    scope: str,
    period_start: date,
    period_end: date,
) -> Optional[dict]:
    validate_scope(scope)
    if _mem() is not None:
        for row in _mem().summaries.values():
            if (
                row["user_id"] == user_id
                and row["scope"] == scope
                and row["period_start"] == period_start
                and row["period_end"] == period_end
            ):
                return deepcopy(row)
        return None

    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, scope, period_start, period_end, summary,
               source_conversation_ids, source_summary_ids,
               model_identifier, created_at
        FROM personalization_summaries
        WHERE user_id = $1::uuid AND scope = $2
          AND period_start = $3::date AND period_end = $4::date
        """,
        user_id, scope, period_start, period_end,
    )
    return dict(row) if row else None


async def list_summaries(
    user_id: str,
    *,
    scopes: tuple[str, ...],
    period_start: date,
    period_end: date,
    limit: int = 100,
) -> list[dict]:
    """Owned summaries of the given scopes fully contained in the period."""
    for scope in scopes:
        validate_scope(scope)
    if _mem() is not None:
        rows = [
            deepcopy(row)
            for row in _mem().summaries.values()
            if row["user_id"] == user_id
            and row["scope"] in scopes
            and row["period_start"] >= period_start
            and row["period_end"] <= period_end
        ]
        rows.sort(key=lambda r: (r["period_start"], r["scope"]))
        return rows[: max(0, int(limit))]

    rows = await db.pool().fetch(
        """
        SELECT id, user_id, scope, period_start, period_end, summary,
               source_conversation_ids, source_summary_ids,
               model_identifier, created_at
        FROM personalization_summaries
        WHERE user_id = $1::uuid
          AND scope = ANY($2::text[])
          AND period_start >= $3::date
          AND period_end <= $4::date
        ORDER BY period_start ASC, scope ASC
        LIMIT $5
        """,
        user_id, list(scopes), period_start, period_end, int(limit),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# prompt_change_proposals (always created pending)
# ---------------------------------------------------------------------------

async def insert_pending_proposal(
    user_id: str,
    *,
    mode: Optional[str],
    proposed_instructions: str,
    reasoning: str,
    evidence: dict,
    risks: Optional[str],
    model_identifier: str,
) -> dict:
    """Insert a status='pending' proposal for human review.

    Raises PendingProposalExistsError when another pending proposal already
    targets the same (user_id, mode) — the DB partial unique index is the
    authoritative guard.
    """
    validate_mode(mode)
    proposed_instructions = (proposed_instructions or "").strip()
    reasoning = (reasoning or "").strip()
    if not proposed_instructions:
        raise ValueError("proposed_instructions must be non-empty")
    if not reasoning:
        raise ValueError("reasoning must be non-empty")
    payload = dict(evidence or {})

    if _mem() is not None:
        for row in _mem().proposals.values():
            if (
                row["user_id"] == user_id
                and row["mode"] == mode
                and row["status"] == "pending"
            ):
                raise PendingProposalExistsError(user_id, mode)
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "mode": mode,
            "proposed_instructions": proposed_instructions,
            "final_instructions": None,
            "reasoning": reasoning,
            "evidence": deepcopy(payload),
            "risks": risks,
            "status": "pending",
            "model_identifier": model_identifier,
            "created_at": _now(),
            "reviewed_at": None,
            "reviewed_by": None,
            "applied_override_id": None,
        }
        _mem().proposals[row["id"]] = row
        return deepcopy(row)

    try:
        row = await db.pool().fetchrow(
            """
            INSERT INTO prompt_change_proposals (
                user_id, mode, proposed_instructions, reasoning,
                evidence, risks, status, model_identifier
            )
            VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6, 'pending', $7)
            RETURNING id, user_id, mode, proposed_instructions, final_instructions,
                      reasoning, evidence, risks, status, model_identifier,
                      created_at, reviewed_at, reviewed_by, applied_override_id
            """,
            user_id, mode, proposed_instructions, reasoning,
            json.dumps(payload, sort_keys=True), risks, model_identifier,
        )
    except asyncpg.UniqueViolationError as exc:
        raise PendingProposalExistsError(user_id, mode) from exc
    return dict(row)


async def get_proposal(proposal_id: str, user_id: str) -> Optional[dict]:
    if _mem() is not None:
        row = _mem().proposals.get(proposal_id)
        if row is None or row["user_id"] != user_id:
            return None
        return deepcopy(row)

    row = await db.pool().fetchrow(
        """
        SELECT id, user_id, mode, proposed_instructions, final_instructions,
               reasoning, evidence, risks, status, model_identifier,
               created_at, reviewed_at, reviewed_by, applied_override_id
        FROM prompt_change_proposals
        WHERE id = $1::uuid AND user_id = $2::uuid
        """,
        proposal_id, user_id,
    )
    return dict(row) if row else None


async def list_pending_proposals(user_id: str, *, limit: int = 50) -> list[dict]:
    if _mem() is not None:
        rows = [
            deepcopy(row)
            for row in _mem().proposals.values()
            if row["user_id"] == user_id and row["status"] == "pending"
        ]
        rows.sort(key=lambda r: r["created_at"])
        return rows[: max(0, int(limit))]

    rows = await db.pool().fetch(
        """
        SELECT id, user_id, mode, proposed_instructions, final_instructions,
               reasoning, evidence, risks, status, model_identifier,
               created_at, reviewed_at, reviewed_by, applied_override_id
        FROM prompt_change_proposals
        WHERE user_id = $1::uuid AND status = 'pending'
        ORDER BY created_at ASC
        LIMIT $2
        """,
        user_id, int(limit),
    )
    return [dict(r) for r in rows]
