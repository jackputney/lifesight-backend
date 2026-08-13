"""Rolling conversation summaries — failure must not break /chat."""

from __future__ import annotations

import logging
from typing import Any, Optional

from shared.context_budget import estimate_tokens
from shared.context_config import context_recent_message_cap, context_summary_threshold

logger = logging.getLogger("lifesight.summary")


def needs_summarization(
    messages_with_seq: list[dict],
    *,
    summary_through_seq: Optional[int],
) -> bool:
    """True when unsummarized message count exceeds configured threshold."""
    threshold = context_summary_threshold()
    if not messages_with_seq:
        return False
    floor = -1 if summary_through_seq is None else int(summary_through_seq)
    unsummarized = [m for m in messages_with_seq if int(m["seq"]) > floor]
    return len(unsummarized) >= threshold


def _content_preview(content: Any, *, limit: int = 400) -> str:
    if isinstance(content, str):
        text = content.strip()
    else:
        text = str(content)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def build_extractive_summary(
    messages_with_seq: list[dict],
    *,
    previous_summary: Optional[str],
    keep_recent: Optional[int] = None,
) -> tuple[str, int]:
    """Summarize older unsummarized span extractively (no LLM required for V1).

    Returns (summary_text, summary_through_seq) where through_seq is the last
    seq included in the summarized older span (exclusive of the recent window).
    """
    keep = keep_recent if keep_recent is not None else context_recent_message_cap()
    if len(messages_with_seq) <= keep:
        # Nothing older to fold — keep prior summary as-is conceptually.
        if previous_summary:
            through = messages_with_seq[-keep - 1]["seq"] if len(messages_with_seq) > keep else (
                messages_with_seq[-1]["seq"] if messages_with_seq else -1
            )
            return previous_summary.strip(), int(through) if messages_with_seq else -1
        return "", -1

    older = messages_with_seq[:-keep]
    recent_boundary_seq = int(older[-1]["seq"])
    lines: list[str] = []
    if previous_summary and previous_summary.strip():
        lines.append(previous_summary.strip())
    lines.append("Additional earlier turns:")
    for msg in older[-40:]:  # bound summary growth
        role = msg.get("role", "user")
        preview = _content_preview(msg.get("content"))
        if preview:
            lines.append(f"- ({role}) {preview}")
    text = "\n".join(lines)
    # Soft cap summary size (~2k tokens ≈ 8k chars at 4 chars/token).
    if estimate_tokens(text) > 2000:
        text = text[:8000].rstrip() + "…"
    return text, recent_boundary_seq


async def maybe_roll_summary(
    *,
    conversation_id: str,
    messages_with_seq: list[dict],
    summary_text: Optional[str],
    summary_through_seq: Optional[int],
    persist,
) -> tuple[Optional[str], Optional[int], bool]:
    """Attempt summarization. On failure, return prior summary unchanged.

    `persist(summary_text, summary_through_seq)` writes to DB.
    Returns (summary_text, summary_through_seq, did_update).
    """
    if not needs_summarization(
        messages_with_seq, summary_through_seq=summary_through_seq
    ):
        return summary_text, summary_through_seq, False
    try:
        new_text, through = build_extractive_summary(
            messages_with_seq,
            previous_summary=summary_text,
        )
        if not new_text.strip():
            return summary_text, summary_through_seq, False
        await persist(new_text, through)
        return new_text, through, True
    except Exception:
        logger.exception(
            "conversation_summary_failed conversation_id=%s", conversation_id
        )
        return summary_text, summary_through_seq, False
