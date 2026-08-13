"""Deterministic open_conversation intent — unique match only, never guess."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

ModeFilter = Optional[str]
DayFilter = Literal["yesterday", "today", "any"]


@dataclass(frozen=True)
class OpenConversationIntent:
    """Parsed reopen-history command (before DB resolution)."""

    mode: ModeFilter
    day: DayFilter
    most_recent: bool
    """True for 'most recent / last chat' style (optionally mode-scoped)."""


@dataclass(frozen=True)
class OpenConversationResolution:
    conversation_id: Optional[str]
    mode: Optional[str]
    ambiguous: bool
    clarify_reply: Optional[str] = None


_MODE_ALIASES = {
    "fitness": "fitness",
    "workout": "fitness",
    "workouts": "fitness",
    "diet": "diet",
    "nutrition": "diet",
    "author": "author",
    "writing": "author",
    "brainstorm": "brainstorm",
    "mail": "mail_calendar",
    "calendar": "mail_calendar",
    "mail calendar": "mail_calendar",
    "mail and calendar": "mail_calendar",
}

# Conservative whole-utterance patterns only.
_PATTERNS = [
    # open my last fitness chat / open my most recent author conversation
    re.compile(
        r"(?is)^\s*(?:please\s+)?"
        r"(?:open|go\s+back\s+to|resume|return\s+to)\s+"
        r"(?:my\s+)?"
        r"(?:(?P<last>last|most\s+recent|latest)\s+)?"
        r"(?:(?P<mode>fitness|workout|workouts|diet|nutrition|author|writing|"
        r"brainstorm|mail|calendar|mail\s+calendar|mail\s+and\s+calendar)\s+)?"
        r"(?:chat|conversation|thread)"
        r"(?:\s+from\s+(?P<day>yesterday|today))?"
        r"\s*[.!?]*\s*$"
    ),
    # open my Author chat from yesterday
    re.compile(
        r"(?is)^\s*(?:please\s+)?"
        r"(?:open|go\s+back\s+to)\s+"
        r"(?:my\s+)?"
        r"(?P<mode>fitness|workout|workouts|diet|nutrition|author|writing|"
        r"brainstorm|mail|calendar|mail\s+calendar|mail\s+and\s+calendar)\s+"
        r"(?:chat|conversation)"
        r"(?:\s+from\s+(?P<day>yesterday|today))?"
        r"\s*[.!?]*\s*$"
    ),
    # open my most recent chat
    re.compile(
        r"(?is)^\s*(?:please\s+)?"
        r"(?:open|go\s+back\s+to)\s+"
        r"(?:my\s+)?"
        r"(?P<last>last|most\s+recent|latest)\s+"
        r"(?:chat|conversation|thread)"
        r"(?:\s+from\s+(?P<day>yesterday|today))?"
        r"\s*[.!?]*\s*$"
    ),
]


def parse_open_conversation_command(transcript: str) -> Optional[OpenConversationIntent]:
    text = transcript or ""
    for pattern in _PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        groups = match.groupdict()
        mode_raw = (groups.get("mode") or "").strip().lower()
        mode = _MODE_ALIASES.get(mode_raw) if mode_raw else None
        day_raw = (groups.get("day") or "").strip().lower()
        day: DayFilter = "any"
        if day_raw == "yesterday":
            day = "yesterday"
        elif day_raw == "today":
            day = "today"
        most_recent = bool(groups.get("last")) or (mode is None and day == "any")
        # "open my author chat from yesterday" — not necessarily "last" keyword
        if mode is not None and not groups.get("last"):
            most_recent = True
        return OpenConversationIntent(mode=mode, day=day, most_recent=most_recent)
    return None


def day_bounds_utc(day: DayFilter, *, now: Optional[datetime] = None) -> Optional[tuple[datetime, datetime]]:
    if day == "any":
        return None
    now = now or datetime.now(timezone.utc)
    local_today = now.astimezone(timezone.utc).date()
    if day == "today":
        start = datetime(local_today.year, local_today.month, local_today.day, tzinfo=timezone.utc)
    else:
        y = local_today - timedelta(days=1)
        start = datetime(y.year, y.month, y.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def resolve_open_conversation(
    candidates: list[dict],
    *,
    intent: OpenConversationIntent,
) -> OpenConversationResolution:
    """Resolve among pre-filtered candidate conversations.

    `candidates` must already be ownership-scoped and preferably mode/day filtered.
    Unique → conversation_id. Zero → clarify. 2+ → ambiguous clarify. Never pick
    arbitrarily among equals.
    """
    if not candidates:
        return OpenConversationResolution(
            conversation_id=None,
            mode=None,
            ambiguous=False,
            clarify_reply=_empty_reply(intent),
        )
    if len(candidates) == 1:
        row = candidates[0]
        return OpenConversationResolution(
            conversation_id=str(row["id"]),
            mode=str(row["mode"]),
            ambiguous=False,
        )
    return OpenConversationResolution(
        conversation_id=None,
        mode=None,
        ambiguous=True,
        clarify_reply=(
            "I found more than one matching conversation. "
            "Tell me the mode and roughly when it was from, "
            "or say open my most recent chat."
        ),
    )


def _empty_reply(intent: OpenConversationIntent) -> str:
    if intent.mode:
        return f"I couldn't find a matching {intent.mode.replace('_', ' ')} conversation."
    return "I couldn't find a matching conversation."


def open_conversation_acknowledgement(mode: Optional[str]) -> str:
    if mode:
        label = mode.replace("_", " ").title()
        return f"Opening your {label} conversation."
    return "Opening that conversation."
