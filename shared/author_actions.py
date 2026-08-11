"""Deterministic Author create commands for /chat (Sprint 4 slice).

Backend performs the Author mutation via shared.author_persistence.store.
client_actions carry *result* signals only — never instructions for iOS to mutate.
No Confirm Gate for create project / create document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from shared.author_persistence import store as author_store
from shared.client_actions import (
    AuthorDocumentCreatedAction,
    AuthorProjectCreatedAction,
    NavigateAction,
    author_document_created_action,
    author_project_created_action,
    empty_client_actions,
)

AuthorCommandKind = Literal["create_project", "create_document"]

# Prefer the more-specific "in <project>" document form first.
# Document title is greedy so "Notes in Progress in My Novel" → project = My Novel.
_CREATE_DOCUMENT_IN_PROJECT = re.compile(
    r"(?is)^\s*(?:please\s+)?"
    r"create\s+(?:an?\s+)?(?:author\s+)?document\s+"
    r"(?:called|named|titled)\s+"
    r"(.+)\s+"
    r"in\s+(?:the\s+)?(?:project\s+)?"
    r"(.+?)\s*[.!?]*\s*$"
)

_CREATE_DOCUMENT_NO_PROJECT = re.compile(
    r"(?is)^\s*(?:please\s+)?"
    r"create\s+(?:an?\s+)?(?:author\s+)?document\s+"
    r"(?:called|named|titled)\s+"
    r"(.+?)\s*[.!?]*\s*$"
)

_CREATE_PROJECT = re.compile(
    r"(?is)^\s*(?:please\s+)?"
    r"create\s+(?:an?\s+)?(?:author\s+)?project\s+"
    r"(?:called|named|titled)\s+"
    r"(.+?)\s*[.!?]*\s*$"
)


@dataclass(frozen=True)
class AuthorCommand:
    kind: AuthorCommandKind
    title: str
    project_title: Optional[str] = None  # required conceptually for create_document


@dataclass(frozen=True)
class AuthorCommandResult:
    reply: str
    client_actions: list[
        NavigateAction | AuthorProjectCreatedAction | AuthorDocumentCreatedAction
    ]
    mutated: bool


def _clean_title(raw: str) -> str:
    value = (raw or "").strip()
    value = value.strip(" \t\"'`“”‘’")
    value = value.rstrip(" .!?,;:")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def parse_author_command(transcript: str) -> Optional[AuthorCommand]:
    """Parse whole-utterance Author create commands. None → not an Author action."""
    text = transcript or ""

    match = _CREATE_DOCUMENT_IN_PROJECT.match(text)
    if match:
        doc_title = _clean_title(match.group(1))
        project_title = _clean_title(match.group(2))
        if not doc_title or not project_title:
            return None
        # Avoid treating "... called X in Y" when X itself ends with " in " noise —
        # the regex already captured the last " in " segment.
        return AuthorCommand(
            kind="create_document", title=doc_title, project_title=project_title
        )

    match = _CREATE_DOCUMENT_NO_PROJECT.match(text)
    if match:
        doc_title = _clean_title(match.group(1))
        if not doc_title:
            return None
        return AuthorCommand(kind="create_document", title=doc_title, project_title=None)

    match = _CREATE_PROJECT.match(text)
    if match:
        title = _clean_title(match.group(1))
        if not title:
            return None
        return AuthorCommand(kind="create_project", title=title)

    return None


async def _projects_matching_title(user_id: str, title: str) -> list[dict]:
    """Case-insensitive exact title match across the user's projects (paged)."""
    needle = title.casefold()
    matches: list[dict] = []
    offset = 0
    page = author_store.MAX_PAGE_LIMIT
    while True:
        rows, total = await author_store.list_projects(user_id, limit=page, offset=offset)
        for row in rows:
            if str(row.get("title") or "").casefold() == needle:
                matches.append(row)
        offset += len(rows)
        if offset >= total or not rows:
            break
    return matches


async def execute_author_command(cmd: AuthorCommand, *, user_id: str) -> AuthorCommandResult:
    """Perform the Author mutation (or clarify). Ownership from JWT user_id only."""
    if cmd.kind == "create_project":
        row = await author_store.create_project(user_id, cmd.title)
        title = str(row["title"])
        return AuthorCommandResult(
            reply=f"I created {title}.",
            client_actions=[
                author_project_created_action(
                    project_id=str(row["id"]),
                    title=title,
                )
            ],
            mutated=True,
        )

    # create_document
    if not cmd.project_title:
        return AuthorCommandResult(
            reply=(
                "Which Author project should I add that document to? "
                "For example: create a document called Chapter One in My Novel."
            ),
            client_actions=empty_client_actions(),
            mutated=False,
        )

    matches = await _projects_matching_title(user_id, cmd.project_title)
    if len(matches) == 0:
        return AuthorCommandResult(
            reply=(
                f"I couldn't find a project called {cmd.project_title}. "
                "Create the project first, or tell me the exact project name."
            ),
            client_actions=empty_client_actions(),
            mutated=False,
        )
    if len(matches) > 1:
        return AuthorCommandResult(
            reply=(
                f"You have more than one project called {cmd.project_title}. "
                "Tell me which one to use, or rename them so the name is unique."
            ),
            client_actions=empty_client_actions(),
            mutated=False,
        )

    project = matches[0]
    doc = await author_store.create_document(
        str(project["id"]), user_id, cmd.title, content=""
    )
    if doc is None:
        return AuthorCommandResult(
            reply="I couldn't create that document in the project.",
            client_actions=empty_client_actions(),
            mutated=False,
        )
    title = str(doc["title"])
    return AuthorCommandResult(
        reply=f"I created {title} in {project['title']}.",
        client_actions=[
            author_document_created_action(
                project_id=str(project["id"]),
                document_id=str(doc["id"]),
                title=title,
            )
        ],
        mutated=True,
    )
