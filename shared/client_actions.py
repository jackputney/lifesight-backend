"""Global app client_actions for /chat.

V1 navigate + Author create *result* signals. Deterministic intent layers run
before mode Claude. Navigation and Author creates never use the Confirm Gate.
jarvis and health are never emit navigate targets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field

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

# Spoken / retired names that must never become navigate targets.
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

# Whole-utterance navigation commands only — conservative V1.
_NAVIGATE_CMD = re.compile(
    r"(?is)^\s*(?:please\s+)?"
    r"(?:open|go\s+to|take\s+me\s+to|switch\s+to|navigate\s+to)"
    r"\s+(.+?)\s*[.!?]*\s*$"
)


class NavigateAction(BaseModel):
    type: Literal["navigate"] = "navigate"
    target: NavigateTarget


class AuthorProjectCreatedAction(BaseModel):
    """Result signal after backend created an Author project (not a mutation command)."""

    type: Literal["author_project_created"] = "author_project_created"
    project_id: str
    title: str


class AuthorDocumentCreatedAction(BaseModel):
    """Result signal after backend created an Author document (not a mutation command)."""

    type: Literal["author_document_created"] = "author_document_created"
    project_id: str
    document_id: str
    title: str


ClientAction = Annotated[
    Union[NavigateAction, AuthorProjectCreatedAction, AuthorDocumentCreatedAction],
    Field(discriminator="type"),
]


@dataclass(frozen=True)
class NavigateMatch:
    """Result of deterministic global navigate intent parsing."""

    target: Optional[NavigateTarget]
    """Allowlisted target, or None when the command named a blocked alias."""

    blocked_alias: Optional[str] = None
    """Normalized blocked name when target is None (health / jarvis)."""


def normalize_command_text(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", (text or "").strip().lower())
    return collapsed


def parse_navigate_command(transcript: str) -> Optional[NavigateMatch]:
    """Return a navigate match for whole-utterance app commands, else None.

    Allowlisted targets become NavigateAction. Blocked aliases
    (health, jarvis) return NavigateMatch(target=None, blocked_alias=...).
    Ordinary chat returns None so mode Claude handles the turn.
    """
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


def author_project_created_action(*, project_id: str, title: str) -> AuthorProjectCreatedAction:
    return AuthorProjectCreatedAction(
        type="author_project_created",
        project_id=str(project_id),
        title=title,
    )


def author_document_created_action(
    *, project_id: str, document_id: str, title: str
) -> AuthorDocumentCreatedAction:
    return AuthorDocumentCreatedAction(
        type="author_document_created",
        project_id=str(project_id),
        document_id=str(document_id),
        title=title,
    )


def navigate_acknowledgement(target: NavigateTarget) -> str:
    label = _SPOKEN_LABELS[target]
    return f"Opening {label}."


def blocked_navigate_reply(alias: str) -> str:
    if alias == "health":
        return "Health isn't available as a mode anymore."
    if alias == "jarvis":
        return "Jarvis isn't available as a mode."
    return "That screen isn't available."


def empty_client_actions() -> list[NavigateAction | AuthorProjectCreatedAction | AuthorDocumentCreatedAction]:
    return []
