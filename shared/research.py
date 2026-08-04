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
from urllib.parse import urlparse

from pydantic import BaseModel, Field

ALLOWED_RESEARCH_STATUSES = frozenset(
    {"not_requested", "completed", "failed", "unavailable"}
)
ALLOWED_VERDICTS = frozenset(
    {"supported", "partially_supported", "not_supported", "inconclusive"}
)

# Provider / public-wire bounds
MAX_RESEARCH_QUERY_LENGTH = 500
MAX_PUBLIC_SOURCES = 5
DEFAULT_RESEARCH_TIMEOUT_SECONDS = 30.0

# Conversational "check" phrasing that is NOT an online verification ask.
_NON_RESEARCH_CHECK = re.compile(
    r"(?i)\b("
    r"check\s+my\s+(reasoning|logic|thinking|idea|ideas|work|math|argument)"
    r"|check\s+this\s+(idea|reasoning|logic|thinking|argument|out)"
    r"|does\s+(?:that|this|the)\s+logic\s+check\s+out"
    r"|logic\s+check\s+out"
    r"|sanity[\s-]?check"
    r"|double[\s-]?check\s+my"
    r")\b"
)

# Explicit online verification / research asks only.
# Bare "check" alone is intentionally insufficient.
_RESEARCH_INTENT = re.compile(
    r"(?i)\b("
    r"fact[\s-]?check(?:\s+\w+){0,6}"
    r"|verify(?:\s+\w+){0,4}\s+online"
    r"|verify\s+this\b"
    r"|verify\s+(?:that|whether|if)\b"
    r"|search\s+the\s+web"
    r"|search\s+online"
    r"|look(?:\s+\w+){0,3}\s+up"
    r"|look\s*up\b"
    r"|research\s+this\b"
    r"|research\s+(?:that|whether|if|the|when|who|what|where|why|how)\b"
    r"|(?:please|can you|could you)\s+research\b"
    r"|check\s+whether\b"
    r"|check\s+if\s+(?:this|that|it)\s+is\s+true"
    r"|check\s+(?:if|whether)\s+this\s+is\s+true"
    r"|check\s+that\s+this\s+is\s+true"
    r"|find\s+sources?\s+for\b"
    r"|find\s+(?:me\s+)?sources?\b"
    r")"
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


_UNSET: object = object()
_provider_override: ResearchProvider | None | object = _UNSET


def set_research_provider_for_tests(provider: ResearchProvider | None) -> None:
    """Test seam. Pass None to force unavailable; reset with clear_…"""
    global _provider_override
    _provider_override = provider


def clear_research_provider_override() -> None:
    global _provider_override
    _provider_override = _UNSET


def get_research_provider() -> ResearchProvider | None:
    """Resolve the active provider. None ⇒ treat as unavailable at call site."""
    if _provider_override is not _UNSET:
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
    """True only for explicit online verification / research asks."""
    text = (transcript or "").strip()
    if not text:
        return False
    if _NON_RESEARCH_CHECK.search(text):
        return False
    return _RESEARCH_INTENT.search(text) is not None


def extract_claim(transcript: str) -> str | None:
    """Best-effort claim string for fact_check when the user asked to verify."""
    text = (transcript or "").strip()
    if not text:
        return None
    if re.search(r"(?i)\bfact[\s-]?check|verify\b|find\s+sources?\b", text):
        return clamp_research_query(text)
    return None


def clamp_research_query(query: str) -> str:
    text = (query or "").strip()
    if len(text) <= MAX_RESEARCH_QUERY_LENGTH:
        return text
    return text[:MAX_RESEARCH_QUERY_LENGTH].rstrip()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_safe_http_url(url: str) -> bool:
    """Accept only http(s) URLs with a non-empty host. Reject other schemes."""
    raw = (url or "").strip()
    if not raw or any(ch.isspace() for ch in raw):
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    # Reject credentials-in-URL oddities and empty hosts after @.
    host = parsed.hostname
    if not host:
        return False
    return True


def sanitize_source_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not is_safe_http_url(raw):
        return None
    return raw


def sanitize_research(
    result: ResearchResult,
    *,
    provider_called: bool,
) -> ResearchResult:
    """Enforce public contract invariants before returning to clients.

    - Unsupported status values are rejected (mapped to failed).
    - fact_check only when status==completed and provider_called.
    - completed requires ≥1 valid http(s) source and a real provider call.
    - At most MAX_PUBLIC_SOURCES are returned.
    - Source snippets are never accepted on the public wire.
    """
    status = result.status if result.status in ALLOWED_RESEARCH_STATUSES else "failed"

    sources: list[ResearchSource] = []
    for raw in result.sources or []:
        title = (raw.title or "").strip() or "Untitled source"
        url = sanitize_source_url(raw.url or "")
        if url is None:
            continue
        sources.append(
            ResearchSource(
                title=title,
                url=url,
                publisher=raw.publisher,
                retrieved_at=raw.retrieved_at or utc_now_iso(),
            )
        )
        if len(sources) >= MAX_PUBLIC_SOURCES:
            break

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

    query = result.query
    if query is not None:
        query = clamp_research_query(query) or None

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
        query=query,
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
                query=clamp_research_query(query) or None,
                summary=None,
                uncertainty="Live web research is not configured.",
                sources=[],
                fact_check=None,
            ),
            provider_called=False,
        ),
    )


def failed_turn(query: str, *, reply: str, uncertainty: str) -> ResearchTurn:
    return ResearchTurn(
        reply=reply,
        research=sanitize_research(
            ResearchResult(
                status="failed",
                query=clamp_research_query(query) or None,
                summary=None,
                uncertainty=uncertainty,
                sources=[],
                fact_check=None,
            ),
            provider_called=True,
        ),
    )
