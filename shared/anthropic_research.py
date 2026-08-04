"""Anthropic native web-search ResearchProvider.

Uses the Messages API server tool `web_search` via httpx rather than the
pinned anthropic==0.42.0 client helpers (that SDK revision only models
client-defined tools with input_schema, not server web_search tool types).
httpx==0.28.1 is already a project dependency.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from shared.research import (
    DEFAULT_RESEARCH_TIMEOUT_SECONDS,
    MAX_PUBLIC_SOURCES,
    ResearchFactCheck,
    ResearchResult,
    ResearchSource,
    ResearchTurn,
    clamp_research_query,
    failed_turn,
    sanitize_research,
    sanitize_source_url,
    utc_now_iso,
)

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 3,
}


class AnthropicResearchProvider:
    """ResearchProvider backed by Anthropic's web_search server tool."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        if timeout is not None:
            self._timeout = timeout
        else:
            raw = os.environ.get("RESEARCH_TIMEOUT_SECONDS", "").strip()
            try:
                self._timeout = float(raw) if raw else DEFAULT_RESEARCH_TIMEOUT_SECONDS
            except ValueError:
                self._timeout = DEFAULT_RESEARCH_TIMEOUT_SECONDS

    async def research(
        self,
        query: str,
        *,
        claim: str | None = None,
    ) -> ResearchTurn:
        q = clamp_research_query(query)
        if not self._api_key:
            research = sanitize_research(
                ResearchResult(
                    status="unavailable",
                    query=q or None,
                    summary=None,
                    uncertainty="ANTHROPIC_API_KEY is not configured for web research.",
                    sources=[],
                    fact_check=None,
                ),
                provider_called=False,
            )
            return ResearchTurn(
                reply=(
                    "I can't look that up right now — web research isn't configured "
                    "on the server. We can keep brainstorming without a live search."
                ),
                research=research,
            )

        system = (
            "You are Olivia doing a single web-research turn for a visually "
            "impaired user. Use the web_search tool. Reply in short spoken-friendly "
            "sentences. State uncertainty clearly. Do not invent sources. "
            "If the user asked to fact-check or verify a claim, end with a single "
            "line exactly in this form:\n"
            "FACT_CHECK: <verdict>| <confidence 0-1>| <short claim>\n"
            "verdict must be one of: supported, partially_supported, not_supported, "
            "inconclusive."
        )
        user_content = q
        if claim:
            user_content = (
                "Fact-check / verify this.\n"
                f"Claim context: {clamp_research_query(claim)}\n\n"
                f"User said: {q}"
            )

        try:
            raw = await self._messages_create(system=system, user_content=user_content)
        except httpx.TimeoutException:
            return failed_turn(
                q,
                reply=(
                    "I tried to look that up, but the web search timed out. "
                    "I have not fact-checked this. We can try again or keep discussing."
                ),
                uncertainty="Web research timed out.",
            )
        except Exception as exc:  # network / API / parse errors
            return failed_turn(
                q,
                reply=(
                    "I tried to look that up, but the web search failed. "
                    "I have not fact-checked this. We can try again or keep discussing."
                ),
                uncertainty=f"Web research failed: {type(exc).__name__}",
            )

        if not isinstance(raw, dict):
            return failed_turn(
                q,
                reply=(
                    "I got an unreadable response from web search, so I have not "
                    "fact-checked this."
                ),
                uncertainty="Malformed research provider response.",
            )

        try:
            sources = self._extract_sources(raw)
            reply_text, fact_line = self._extract_text_and_fact_line(raw)
        except Exception:
            return failed_turn(
                q,
                reply=(
                    "I got a malformed research response, so I have not fact-checked "
                    "this."
                ),
                uncertainty="Malformed research provider response.",
            )

        if not reply_text:
            reply_text = (
                "I finished a web search, but didn't get a usable summary. "
                "See the sources if any were returned."
            )

        fact_check = None
        if fact_line and sources:
            fact_check = self._parse_fact_line(fact_line, default_claim=claim or q)

        if not sources:
            return failed_turn(
                q,
                reply=(
                    "I ran a web search but couldn't get citable sources, "
                    "so I have not fact-checked this."
                ),
                uncertainty="The search completed without usable http(s) sources.",
            )

        research = sanitize_research(
            ResearchResult(
                status="completed",
                query=q or None,
                summary=reply_text,
                uncertainty=(
                    "Findings depend on the cited sources and may be incomplete "
                    "or contested."
                ),
                sources=sources,
                fact_check=fact_check,
            ),
            provider_called=True,
        )
        # sanitize may still demote to failed if every URL was rejected.
        if research.status != "completed":
            return ResearchTurn(
                reply=(
                    "I ran a web search but couldn't get citable sources, "
                    "so I have not fact-checked this."
                ),
                research=research,
            )
        return ResearchTurn(reply=reply_text, research=research)

    async def _messages_create(self, *, system: str, user_content: str) -> dict[str, Any]:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self._model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            "tools": [WEB_SEARCH_TOOL],
        }
        timeout = httpx.Timeout(self._timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(ANTHROPIC_MESSAGES_URL, headers=headers, json=body)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Anthropic research HTTP {resp.status_code}: {resp.text[:300]}"
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise RuntimeError("Anthropic research returned non-JSON body") from exc
            if not isinstance(data, dict):
                raise RuntimeError("Anthropic research JSON was not an object")
            return data

    def _extract_sources(self, payload: dict[str, Any]) -> list[ResearchSource]:
        retrieved = utc_now_iso()
        found: list[ResearchSource] = []
        seen_urls: set[str] = set()
        content = payload.get("content")
        if not isinstance(content, list):
            return []

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "web_search_tool_result":
                result_content = block.get("content")
                if isinstance(result_content, list):
                    for item in result_content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "web_search_result":
                            src = self._source_from_result(item, retrieved)
                            if src and src.url not in seen_urls:
                                seen_urls.add(src.url)
                                found.append(src)
                                if len(found) >= MAX_PUBLIC_SOURCES:
                                    return found
            if btype == "text":
                for cit in block.get("citations") or []:
                    if not isinstance(cit, dict):
                        continue
                    src = self._source_from_citation(cit, retrieved)
                    if src and src.url not in seen_urls:
                        seen_urls.add(src.url)
                        found.append(src)
                        if len(found) >= MAX_PUBLIC_SOURCES:
                            return found
        return found

    def _source_from_result(self, item: dict[str, Any], retrieved: str) -> ResearchSource | None:
        url = sanitize_source_url(str(item.get("url") or ""))
        if url is None:
            return None
        title = (str(item.get("title") or "")).strip() or url
        return ResearchSource(
            title=title,
            url=url,
            publisher=self._publisher_from_url(url),
            retrieved_at=retrieved,
        )

    def _source_from_citation(self, cit: dict[str, Any], retrieved: str) -> ResearchSource | None:
        url = sanitize_source_url(str(cit.get("url") or ""))
        if url is None:
            return None
        title = (str(cit.get("title") or "")).strip() or url
        return ResearchSource(
            title=title,
            url=url,
            publisher=self._publisher_from_url(url),
            retrieved_at=retrieved,
        )

    @staticmethod
    def _publisher_from_url(url: str) -> str | None:
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            return None
        if host.startswith("www."):
            host = host[4:]
        return host or None

    def _extract_text_and_fact_line(self, payload: dict[str, Any]) -> tuple[str, str | None]:
        texts: list[str] = []
        content = payload.get("content")
        if not isinstance(content, list):
            return "", None
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = (block.get("text") or "").strip()
                if t:
                    texts.append(t)
        combined = "\n".join(texts).strip()
        fact_line = None
        m = re.search(r"(?im)^FACT_CHECK:\s*(.+)$", combined)
        if m:
            fact_line = m.group(1).strip()
            combined = re.sub(r"(?im)^FACT_CHECK:\s*.+$", "", combined).strip()
        return combined, fact_line

    def _parse_fact_line(self, line: str, *, default_claim: str) -> ResearchFactCheck | None:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            return None
        verdict = parts[0].lower().replace(" ", "_")
        try:
            confidence = float(parts[1])
        except ValueError:
            confidence = 0.5
        claim = parts[2] if len(parts) > 2 and parts[2] else default_claim
        return ResearchFactCheck(claim=claim, verdict=verdict, confidence=confidence)
