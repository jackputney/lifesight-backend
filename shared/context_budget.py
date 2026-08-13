"""Token-aware conversation context assembly for Claude.

Invariant: assembled messages stay under CONTEXT_INPUT_TOKEN_BUDGET using a
chars/token estimate, with CONTEXT_RECENT_MESSAGE_CAP as a secondary max.
Full raw history remains in Postgres regardless of what is sent to the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from shared.context_config import (
    context_chars_per_token,
    context_input_token_budget,
    context_recent_message_cap,
)


def estimate_tokens(text: str) -> int:
    chars = max(len(text or ""), 1)
    per = context_chars_per_token()
    return max(1, int(chars / per))


def _content_as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except TypeError:
        return str(content)


def message_token_estimate(message: dict) -> int:
    return estimate_tokens(_content_as_text(message.get("content")))


@dataclass(frozen=True)
class BuiltContext:
    messages: list[dict]
    raw_messages_included: int
    summary_used: bool
    summary_through_seq: Optional[int]
    estimated_message_tokens: int
    estimated_system_tokens: int
    approx_context_utilization: float


def build_model_messages(
    *,
    system_prompt: str,
    profile_block: str,
    summary_text: Optional[str],
    summary_through_seq: Optional[int],
    recent_messages: list[dict],
    current_user_message: dict,
) -> BuiltContext:
    """Assemble Anthropic `messages` under the configured input budget.

    `recent_messages` should already exclude the current turn (caller appends
    current_user_message last). Older history is represented only via summary.
    """
    budget = context_input_token_budget()
    cap = context_recent_message_cap()

    system_tokens = estimate_tokens(system_prompt) + estimate_tokens(profile_block)
    summary_used = bool((summary_text or "").strip())
    summary_tokens = estimate_tokens(summary_text or "") if summary_used else 0
    current_tokens = message_token_estimate(current_user_message)

    reserved = system_tokens + summary_tokens + current_tokens
    remaining = max(0, budget - reserved)

    # Take at most `cap` most-recent prior messages, then trim from the oldest
    # end until the estimate fits.
    window = list(recent_messages[-cap:]) if cap else []
    while window and sum(message_token_estimate(m) for m in window) > remaining:
        window.pop(0)

    # Prefix a synthetic user note with the rolling summary when present.
    assembled: list[dict] = []
    if summary_used:
        assembled.append(
            {
                "role": "user",
                "content": (
                    "Conversation summary of earlier turns "
                    "(verbatim recent messages follow):\n"
                    f"{summary_text.strip()}"
                ),
            }
        )
        assembled.append(
            {
                "role": "assistant",
                "content": "Understood. I'll use that summary plus the recent messages.",
            }
        )

    assembled.extend(window)
    assembled.append(current_user_message)

    msg_tokens = sum(message_token_estimate(m) for m in assembled)
    total = system_tokens + msg_tokens
    utilization = min(1.0, total / float(budget)) if budget else 1.0

    return BuiltContext(
        messages=assembled,
        raw_messages_included=len(window) + 1,  # includes current turn
        summary_used=summary_used,
        summary_through_seq=summary_through_seq if summary_used else None,
        estimated_message_tokens=msg_tokens,
        estimated_system_tokens=system_tokens,
        approx_context_utilization=utilization,
    )
