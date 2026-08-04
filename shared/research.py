"""Brainstorm research: provider protocol, intent detection, wire sanitization.

Public `research` on ChatResponse stays separate from `visual_panel`.
No Confirm Gate — research is read-only.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

ALLOWED_RESEARCH_STATUSES = frozenset(
    {"not_requested", "completed", "failed", "unavailable"}
)
ALLOWED_VERDICTS = frozenset(
    {"supported", "partially_supported", "not_supported", "inconclusive"}
)

# Explicit research / verification asks only — ordinary brainstorm discussion
# must not trigger a provider call.
_RESEARCH_INTENT = re.compile(
    r"(?i)\b("
    r"fact[\s-]?check(?:ing|ed|s)?"
    r"|verify(?:ing|ied|ies)?"
    r"|research(?:ing|ed)?"
    r"|look(?:ing)?\s+(?:\w+\s+){0,3}up"
    r"|look\s*up"
    r"|check(?:ing|ed|s)?"
    r")\b"
)


class ResearchSource(BaseModel):
    title: str
    url: str
    publisher: str | None = None
    retrieved_at: str


class ResearchFactCheck(BaseModel):
    claim: str
    verdict: str
    confidence: float


class ResearchResult(BaseModel):
    status: str
    query: str | None = None
    summary: str | None = None
    uncertainty: str | None = None
    sources: list[ResearchSource] = Field(default_factory=list)
    fact_check: ResearchFactCheck | None = None


@dataclass
class ResearchTurn:
    """Spoken reply plus a non-null research object for an explicit research ask."""

    reply: str
    research: ResearchResult


@runtime_checkable
class ResearchProvider(Protocol):
    """Pluggable web research backend (Anthropic first; Tavily/Brave later)."""

    async def research(
        self,
        query: str,
        *,
        claim: str | None = None,
    ) -> ResearchTurn:
        """Run a real web-search operation and return reply + research payload."""
        ...


_provider_override: ResearchProvider | None | object = object()


def set_research_provider_for_tests(provider: ResearchProvider | None) -> None:
    """Test seam. Pass None to force unavailable; omit reset via clear_…"""
    global _provider_override
    _provider_override = provider


def clear_research_provider_override() -> None:
    global _provider_override
    _provider_override = object()


def get_research_provider() -> ResearchProvider | None:
    """Resolve the active provider. None ⇒ treat as unavailable at call site."""
    if _provider_override is not object():
        return _provider_override  # type: ignore[return-value]

    name = (os.environ.get("RESEARCH_PROVIDER") or "anthropic").strip().lower()
    if name in ("", "none", "off", "disabled"):
        return None
    if name == "anthropic":
        # Lazy import so unit tests with a fake never need Anthropic/httpx wiring.
        from shared.anthropic_research import AnthropicResearchProvider

        return AnthropicResearchProvider()
    return None


def wants_research(transcript: str) -> bool:
    text = (transcript or "").strip()
    if not text:
        return False
    return _RESEARCH_INTENT.search(text) is not None


def extract_claim(transcript: str) -> str | None:
    """Best-effort claim string for fact_check when the user asked to verify."""
    text = (transcript or "").strip()
    if not text:
        return None
    if re.search(r"(?i)\bfact[\s-]?check|verify\b", text):
        return text
    return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_research(
    result: ResearchResult,
    *,
    provider_called: bool,
) -> ResearchResult:
    """Enforce public contract invariants before returning to clients.

    - Unsupported status values are rejected (mapped to failed).
    - fact_check only when status==completed and provider_called.
    - completed requires ≥1 source and a real provider call.
    - Source snippets are never accepted on the public wire.
    """
    status = result.status if result.status in ALLOWED_RESEARCH_STATUSES else "failed"

    sources: list[ResearchSource] = []
    for raw in result.sources or []:
        # Drop any accidental snippet-like extras by reconstructing the public shape.
        title = (raw.title or "").strip() or "Untitled source"
        url = (raw.url or "").strip()
        if not url:
            continue
        sources.append(
            ResearchSource(
                title=title,
                url=url,
                publisher=raw.publisher,
                retrieved_at=raw.retrieved_at or utc_now_iso(),
            )
        )

    fact_check = result.fact_check
    if fact_check is not None:
        verdict = fact_check.verdict if fact_check.verdict in ALLOWED_VERDICTS else "inconclusive"
        conf = fact_check.confidence
        if conf < 0.0:
            conf = 0.0
        if conf > 1.0:
            conf = 1.0
        fact_check = ResearchFactCheck(
            claim=(fact_check.claim or "").strip() or (result.query or "claim"),
            verdict=verdict,
            confidence=conf,
        )

    if not provider_called:
        # No real web-search op ⇒ never completed / never fact_check.
        if status == "completed":
            status = "failed"
        fact_check = None
    elif status == "completed":
        if not sources:
            status = "failed"
            fact_check = None
    else:
        # failed / unavailable / not_requested
        fact_check = None

    if status != "completed":
        fact_check = None

    return ResearchResult(
        status=status,
        query=result.query,
        summary=result.summary,
        uncertainty=result.uncertainty,
        sources=sources if status == "completed" else [],
        fact_check=fact_check,
    )


def unavailable_turn(query: str, *, reason: str) -> ResearchTurn:
    return ResearchTurn(
        reply=reason,
        research=sanitize_research(
            ResearchResult(
                status="unavailable",
                query=query,
                summary=None,
                uncertainty="Live web research is not configured.",
                sources=[],
                fact_check=None,
            ),
            provider_called=False,
        ),
    )
