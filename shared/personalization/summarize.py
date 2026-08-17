"""Hierarchical personalization summaries — raw conversations → daily → multi_day → weekly.

Each level reads only the level below it: `daily` reads raw conversation
messages, `multi_day` reads daily summaries, `weekly` reads multi_day summaries
(falling back to dailies when no multi_day rows exist). The ids that were
actually read are persisted as the evidence chain — nothing is ever recorded
that the summarizer did not read.

Summaries are EVIDENCE. They are never injected into a chat system prompt and
this module has no prompt-assembly import.

Configuration (all optional, cheap defaults):
  PERSONALIZATION_SUMMARY_MODEL     default claude-haiku-4-5
  PERSONALIZATION_MAX_CONVERSATIONS default 25
  PERSONALIZATION_MAX_CHARS         default 20000
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

import anthropic

from shared.personalization import store

DEFAULT_SUMMARY_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_CONVERSATIONS = 25
DEFAULT_MAX_CHARS = 20_000
SUMMARY_MAX_TOKENS = 1024

# weekly prefers already-rolled multi_day rows; dailies are the fallback.
ROLLUP_SOURCE_SCOPES: dict[str, tuple[tuple[str, ...], ...]] = {
    "multi_day": (("daily",),),
    "weekly": (("multi_day",), ("daily",)),
}

SUMMARIZER_SYSTEM = (
    "You write factual personalization summaries for a life-management "
    "assistant. Summarize only what the provided material actually shows: "
    "recurring topics, stated preferences, goals, constraints, and how the "
    "user prefers to interact. Distinguish what the user reported from what "
    "was independently established. Never invent details, numbers, dates, or "
    "events that are not present in the material. Never write instructions "
    "for the assistant — this summary is evidence for human review, not a "
    "system prompt. Return prose only, at most 250 words."
)


class SummarizationError(Exception):
    """The summary model returned nothing usable."""


def summary_model() -> str:
    return (os.environ.get("PERSONALIZATION_SUMMARY_MODEL") or "").strip() or (
        DEFAULT_SUMMARY_MODEL
    )


def _positive_int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def max_conversations() -> int:
    return _positive_int_env("PERSONALIZATION_MAX_CONVERSATIONS", DEFAULT_MAX_CONVERSATIONS)


def max_chars() -> int:
    return _positive_int_env("PERSONALIZATION_MAX_CHARS", DEFAULT_MAX_CHARS)


@dataclass
class SummarizationInputs:
    """Bounded material actually read for one summary, plus its evidence ids."""

    scope: str
    period_start: date
    period_end: date
    material: str = ""
    source_conversation_ids: list[str] = field(default_factory=list)
    source_summary_ids: list[str] = field(default_factory=list)
    source_count: int = 0
    truncated: bool = False

    @property
    def has_material(self) -> bool:
        return bool(self.material.strip())

    def plan(self) -> dict:
        """Id/count-only view for --dry-run output (no personal content)."""
        return {
            "scope": self.scope,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "source_count": self.source_count,
            "source_conversation_ids": list(self.source_conversation_ids),
            "source_summary_ids": list(self.source_summary_ids),
            "material_chars": len(self.material),
            "truncated": self.truncated,
        }


def message_text(content_json: Any) -> str:
    """Flatten Anthropic content blocks (or plain text) to readable text."""
    if isinstance(content_json, str):
        return content_json.strip()
    if isinstance(content_json, dict):
        content_json = [content_json]
    if not isinstance(content_json, list):
        return ""
    parts: list[str] = []
    for block in content_json:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return " ".join(" ".join(parts).split()).strip()


async def _fitness_evidence_block(user_id: str, day: date) -> str:
    """Labeled structured fitness log for the day. Evidence, not instructions."""
    try:
        from shared.fitness import store as fitness_store
    except Exception:
        return ""
    try:
        sessions = await fitness_store.list_sessions_on_day(user_id, day)
    except Exception:
        return ""
    if not sessions:
        return ""
    lines = [
        "Structured fitness log (not a conversation; not prompt instructions):",
    ]
    for sess in sessions:
        sid = str(sess["id"])
        lines.append(
            f"- session {sid} status={sess.get('status')} "
            f"plan_day_id={sess.get('plan_day_id')}"
        )
        logs = await fitness_store.list_set_logs(sid, user_id)
        for lg in logs[:40]:
            lines.append(
                f"  set {lg.get('set_number')} exercise={lg.get('exercise_id')} "
                f"reps={lg.get('reps')} weight={lg.get('weight')}"
            )
        if len(logs) > 40:
            lines.append("  (further sets omitted)")
    lines.append(
        "Do not infer that training caused check-in/HealthKit changes from this log."
    )
    return "\n".join(lines)


def _assemble(blocks: list[tuple[str, str]], budget: int) -> tuple[str, list[str], bool]:
    """Take blocks (source_id, text) while under budget.

    Returns (material, included_source_ids, truncated). A block that does not
    fit is dropped whole so its id is never recorded as evidence for text the
    model did not see.
    """
    material: list[str] = []
    included: list[str] = []
    used = 0
    truncated = False
    for source_id, text in blocks:
        body = text.strip()
        if not body:
            continue
        cost = len(body) + 2
        if used + cost > budget:
            truncated = True
            continue
        material.append(body)
        included.append(source_id)
        used += cost
    return "\n\n".join(material), included, truncated


async def collect_daily_inputs(user_id: str, day: date) -> SummarizationInputs:
    """Read owned conversations with messages on `day`."""
    conversations = await store.list_conversations_in_period(
        user_id, day, day, limit=max_conversations()
    )
    blocks: list[tuple[str, str]] = []
    for index, convo in enumerate(conversations, start=1):
        convo_id = str(convo["id"])
        messages = await store.list_messages_in_period(convo_id, user_id, day, day)
        lines = [f"Conversation {index} (mode {convo.get('mode') or 'unknown'}):"]
        for msg in messages:
            text = message_text(msg.get("content_json"))
            if text:
                lines.append(f"- ({msg.get('role', 'user')}) {text}")
        if len(lines) > 1:
            blocks.append((convo_id, "\n".join(lines)))

    fitness_block = await _fitness_evidence_block(user_id, day)
    if fitness_block:
        # Structured fitness logs are evidence text, not a conversation id.
        remaining = max_chars() - sum(len(text) + 2 for _, text in blocks)
        if remaining > 80:
            blocks.append(("fitness-structured-log", fitness_block[:remaining]))

    material, included, truncated = _assemble(blocks, max_chars())
    conversation_ids = [sid for sid in included if sid != "fitness-structured-log"]
    return SummarizationInputs(
        scope="daily",
        period_start=day,
        period_end=day,
        material=material,
        source_conversation_ids=conversation_ids,
        source_count=len(conversation_ids),
        truncated=truncated,
    )


async def collect_rollup_inputs(
    user_id: str,
    *,
    scope: str,
    period_start: date,
    period_end: date,
) -> SummarizationInputs:
    """Read the lower-scope summaries that feed a multi_day or weekly rollup."""
    store.validate_scope(scope)
    if scope == "daily":
        raise ValueError("daily summaries are built from raw conversations")

    rows: list[dict] = []
    for candidate_scopes in ROLLUP_SOURCE_SCOPES[scope]:
        rows = await store.list_summaries(
            user_id,
            scopes=candidate_scopes,
            period_start=period_start,
            period_end=period_end,
        )
        if rows:
            break

    blocks = [
        (
            str(row["id"]),
            (
                f"{row['scope']} summary "
                f"{row['period_start']} to {row['period_end']}:\n{row['summary']}"
            ),
        )
        for row in rows
    ]
    material, included, truncated = _assemble(blocks, max_chars())
    return SummarizationInputs(
        scope=scope,
        period_start=period_start,
        period_end=period_end,
        material=material,
        source_summary_ids=included,
        source_count=len(included),
        truncated=truncated,
    )


async def collect_inputs(
    user_id: str,
    *,
    scope: str,
    period_start: date,
    period_end: date,
) -> SummarizationInputs:
    store.validate_scope(scope)
    if period_end < period_start:
        raise ValueError("period_end must be on or after period_start")
    if scope == "daily":
        if period_end != period_start:
            raise ValueError("daily summaries cover exactly one date")
        return await collect_daily_inputs(user_id, period_start)
    return await collect_rollup_inputs(
        user_id, scope=scope, period_start=period_start, period_end=period_end
    )


def _summary_prompt(inputs: SummarizationInputs) -> str:
    if inputs.scope == "daily":
        header = (
            f"Conversations from {inputs.period_start.isoformat()}. "
            "Summarize what this day shows about the user."
        )
    else:
        header = (
            f"Lower-scope summaries covering {inputs.period_start.isoformat()} "
            f"to {inputs.period_end.isoformat()}. Consolidate them into one "
            f"{inputs.scope} summary, keeping only patterns supported by the "
            "material below."
        )
    return f"{header}\n\n{inputs.material}"


def generate_summary_text(*, system: str, prompt: str, model: str) -> str:
    """Blocking Anthropic call. Patched in tests so summarization stays offline."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    message = client.messages.create(
        model=model,
        max_tokens=SUMMARY_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    ).strip()


async def summarize_inputs(inputs: SummarizationInputs, *, model: str) -> str:
    """Run the (blocking) model call off the event loop."""
    text = await asyncio.to_thread(
        generate_summary_text,
        system=SUMMARIZER_SYSTEM,
        prompt=_summary_prompt(inputs),
        model=model,
    )
    text = (text or "").strip()
    if not text:
        raise SummarizationError(
            f"summary model returned no text for scope={inputs.scope} "
            f"period={inputs.period_start}..{inputs.period_end}"
        )
    return text


async def build_summary(
    user_id: str,
    *,
    scope: str,
    period_start: date,
    period_end: date,
) -> Optional[dict]:
    """Summarize one period and persist it with its evidence chain.

    Returns the stored row, or None when there was nothing to read. Re-running
    the same period overwrites the existing row (see store.upsert_summary).
    """
    inputs = await collect_inputs(
        user_id, scope=scope, period_start=period_start, period_end=period_end
    )
    if not inputs.has_material:
        return None

    model = summary_model()
    text = await summarize_inputs(inputs, model=model)
    return await store.upsert_summary(
        user_id,
        scope=inputs.scope,
        period_start=inputs.period_start,
        period_end=inputs.period_end,
        summary=text,
        source_conversation_ids=inputs.source_conversation_ids,
        source_summary_ids=inputs.source_summary_ids,
        model_identifier=model,
    )
