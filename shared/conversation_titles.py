"""Conservative V1 conversation titles — no LLM."""

from __future__ import annotations

import re

from shared.context_config import CONVERSATION_TITLE_MAX_CHARS

_MODE_LABELS = {
    "fitness": "Fitness",
    "diet": "Diet",
    "author": "Author",
    "brainstorm": "Brainstorm",
    "mail_calendar": "Mail and Calendar",
    "jarvis": "Jarvis",
}


def fallback_title(mode: str) -> str:
    label = _MODE_LABELS.get((mode or "").lower(), (mode or "Chat").capitalize())
    return f"{label} chat"


def title_from_user_text(text: str, *, mode: str) -> str:
    """First substantive user message, normalized/truncated ~60 chars."""
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    # Skip pure navigate-like acknowledgements as "substantive" if empty after strip.
    if len(collapsed) < 2:
        return fallback_title(mode)
    if len(collapsed) <= CONVERSATION_TITLE_MAX_CHARS:
        return collapsed
    cut = collapsed[: CONVERSATION_TITLE_MAX_CHARS - 1].rstrip()
    return f"{cut}…"
