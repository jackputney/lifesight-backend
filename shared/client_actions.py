"""Global app client_actions for /chat — navigate + open_conversation.

Deterministic intent layer runs before mode Claude. These actions never use the
Confirm Gate. jarvis and health are never emit targets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

ClientActionType = Literal["navigate", "open_conversation"]
NavigateTarget = Literal[
    "home",
    "fitness",
    "diet",
    "author",
    "brainstorm",
    "mail_calendar",
    "settings",
]

ALLOWED_NAVIGATE_TARGETS: frozenset[str] = frozenset(
    {
        "home",
        "fitness",
        "diet",
        "author",
        "brainstorm",
        "mail_calendar",
        "settings",
    }
)

BLOCKED_NAVIGATE_ALIASES: frozenset[str] = frozenset(
    {
        "health",
        "jarvis",
    }
)

_TARGET_ALIASES: dict[str, NavigateTarget] = {
    "home": "home",
    "main": "home",
    "start": "home",
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
    "settings": "settings",
    "setting": "settings",
}

_SPOKEN_LABELS: dict[NavigateTarget, str] = {
    "home": "Home",
    "fitness": "Fitness",
    "diet": "Diet",
    "author": "Author",
    "brainstorm": "Brainstorm",
    "mail_calendar": "Mail and Calendar",
    "settings": "Settings",
}

_NAVIGATE_CMD = re.compile(
    r"(?is)^\s*(?:please\s+)?"
    r"(?:open|go\s+to|take\s+me\s+to|switch\s+to|navigate\s+to)"
    r"\s+(.+?)\s*[.!?]*\s*$"
)


class NavigateAction(BaseModel):
    type: Literal["navigate"] = "navigate"
    target: NavigateTarget


class OpenConversationAction(BaseModel):
    type: Literal["open_conversation"] = "open_conversation"
    conversation_id: str

    @field_validator("conversation_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        return str(UUID(str(value)))


ClientAction = Annotated[
    Union[NavigateAction, OpenConversationAction],
    Field(discriminator="type"),
]


@dataclass(frozen=True)
class NavigateMatch:
    target: Optional[NavigateTarget]
    blocked_alias: Optional[str] = None


def normalize_command_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def parse_navigate_command(transcript: str) -> Optional[NavigateMatch]:
    """Whole-utterance app screen navigation. History reopen is handled separately."""
    lowered = normalize_command_text(transcript or "")
    if re.search(r"\b(chat|conversation|thread)\b", lowered):
        return None
    match = _NAVIGATE_CMD.match(transcript or "")
    if not match:
        return None
    raw = normalize_command_text(match.group(1))
    raw = raw.strip(" .!?,;:'\"")
    if not raw:
        return None
    if raw in BLOCKED_NAVIGATE_ALIASES:
        return NavigateMatch(target=None, blocked_alias=raw)
    target = _TARGET_ALIASES.get(raw)
    if target is None:
        return None
    if target not in ALLOWED_NAVIGATE_TARGETS:
        return None
    return NavigateMatch(target=target)


def navigate_action(target: NavigateTarget) -> NavigateAction:
    if target not in ALLOWED_NAVIGATE_TARGETS:
        raise ValueError(f"navigate target not allowlisted: {target}")
    return NavigateAction(type="navigate", target=target)


def open_conversation_action(conversation_id: str) -> OpenConversationAction:
    return OpenConversationAction(
        type="open_conversation", conversation_id=str(conversation_id)
    )


def navigate_acknowledgement(target: NavigateTarget) -> str:
    return f"Opening {_SPOKEN_LABELS[target]}."


def blocked_navigate_reply(alias: str) -> str:
    if alias == "health":
        return "Health isn't available as a mode anymore."
    if alias == "jarvis":
        return "Jarvis isn't available as a mode."
    return "That screen isn't available."


def empty_client_actions() -> list:
    return []
