"""Brainstorm research provider wiring — fake provider only (no live paid search).

Run:  python -m unittest tests.test_brainstorm_research -v
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from main import app
from shared.anthropic_research import AnthropicResearchProvider
from shared.research import (
    MAX_PUBLIC_SOURCES,
    MAX_RESEARCH_QUERY_LENGTH,
    ResearchFactCheck,
    ResearchResult,
    ResearchSource,
    ResearchTurn,
    clamp_research_query,
    clear_research_provider_override,
    is_safe_http_url,
    sanitize_research,
    sanitize_source_url,
    set_research_provider_for_tests,
    utc_now_iso,
    wants_research,
)


class FakeResearchProvider:
    def __init__(self, turn: ResearchTurn | Exception) -> None:
        self._turn = turn
        self.calls: list[tuple[str, str | None]] = []

    async def research(self, query: str, *, claim: str | None = None) -> ResearchTurn:
        self.calls.append((query, claim))
        if isinstance(self._turn, Exception):
            raise self._turn
        return self._turn


def _completed_turn(query: str = "fact-check this: the sky is blue") -> ResearchTurn:
    return ResearchTurn(
        reply="Sources say the daytime sky appears blue due to Rayleigh scattering.",
        research=sanitize_research(
            ResearchResult(
                status="completed",
                query=query,
                summary="Daytime sky appears blue due to Rayleigh scattering.",
                uncertainty="Simplified popular-science summary.",
                sources=[
                    ResearchSource(
                        title="Rayleigh scattering",
                        url="https://example.com/rayleigh",
                        publisher="example.com",
                        retrieved_at=utc_now_iso(),
                    )
                ],
                fact_check=ResearchFactCheck(
                    claim="The sky is blue",
                    verdict="supported",
                    confidence=0.8,
                ),
            ),
            provider_called=True,
        ),
    )


class IntentTests(unittest.TestCase):
    def test_ordinary_discussion_does_not_want_research(self) -> None:
        self.assertFalse(wants_research("What if the mentor is lying?"))
        self.assertFalse(wants_research("Help me poke holes in this idea."))
        self.assertFalse(wants_research("Please check my reasoning on this plot beat."))
        self.assertFalse(wants_research("Can you check this idea with me?"))
        self.assertFalse(wants_research("Does that logic check out?"))
        self.assertFalse(wants_research("Just check."))  # bare check insufficient
        self.assertFalse(wants_research("I'll check tomorrow."))

    def test_explicit_phrases_want_research(self) -> None:
        self.assertTrue(wants_research("Fact-check this: the FDA was founded in 1906."))
        self.assertTrue(wants_research("Please verify this online."))
        self.assertTrue(wants_research("Search the web for when the FDA was founded."))
        self.assertTrue(wants_research("Look this up for me."))
        self.assertTrue(wants_research("Research this claim about boiling water."))
        self.assertTrue(wants_research("Check whether this is true: water boils at 100C."))
        self.assertTrue(wants_research("Find sources for this claim."))
        self.assertTrue(wants_research("Can you research when the FDA was founded?"))
        self.assertTrue(wants_research("Verify this claim for me."))
        self.assertTrue(wants_research("Look up the founding year of the FDA."))
        self.assertTrue(wants_research("Check whether water boils at 100C at sea level."))


class UrlSanitizeTests(unittest.TestCase):
    def test_only_http_https_allowed(self) -> None:
        self.assertTrue(is_safe_http_url("https://example.com/a"))
        self.assertTrue(is_safe_http_url("http://example.com/a"))
        self.assertFalse(is_safe_http_url("javascript:alert(1)"))
        self.assertFalse(is_safe_http_url("data:text/html,hi"))
        self.assertFalse(is_safe_http_url("file:///etc/passwd"))
        self.assertFalse(is_safe_http_url("ftp://example.com/a"))
        self.assertFalse(is_safe_http_url("not a url"))
        self.assertFalse(is_safe_http_url("https://"))
        self.assertIsNone(sanitize_source_url("javascript:void(0)"))
        self.assertEqual(sanitize_source_url("https://ok.example/x"), "https://ok.example/x")

    def test_sanitize_drops_unsafe_urls_and_caps_sources(self) -> None:
        sources = [
            ResearchSource(
                title="bad",
                url="javascript:alert(1)",
                publisher=None,
                retrieved_at=utc_now_iso(),
            ),
            ResearchSource(
                title="file",
                url="file:///tmp/x",
                publisher=None,
                retrieved_at=utc_now_iso(),
            ),
        ]
        for i in range(8):
            sources.append(
                ResearchSource(
                    title=f"ok{i}",
                    url=f"https://example.com/{i}",
                    publisher="example.com",
                    retrieved_at=utc_now_iso(),
                )
            )
        out = sanitize_research(
            ResearchResult(status="completed", query="q", sources=sources),
            provider_called=True,
        )
        self.assertEqual(out.status, "completed")
        self.assertEqual(len(out.sources), MAX_PUBLIC_SOURCES)
        self.assertTrue(all(s.url.startswith("https://") for s in out.sources))

    def test_completed_with_only_unsafe_urls_becomes_failed(self) -> None:
        out = sanitize_research(
            ResearchResult(
                status="completed",
                query="q",
                sources=[
                    ResearchSource(
                        title="bad",
                        url="data:text/plain,hi",
                        publisher=None,
                        retrieved_at=utc_now_iso(),
                    )
                ],
            ),
            provider_called=True,
        )
        self.assertEqual(out.status, "failed")
        self.assertEqual(out.sources, [])


class SanitizeTests(unittest.TestCase):
    def test_unsupported_status_becomes_failed(self) -> None:
        out = sanitize_research(
            ResearchResult(status="running", query="q", sources=[]),
            provider_called=True,
        )
        self.assertEqual(out.status, "failed")
        self.assertIsNone(out.fact_check)

    def test_completed_without_sources_becomes_failed(self) -> None:
        out = sanitize_research(
            ResearchResult(
                status="completed",
                query="q",
                sources=[],
                fact_check=ResearchFactCheck(
                    claim="c", verdict="supported", confidence=0.9
                ),
            ),
            provider_called=True,
        )
        self.assertEqual(out.status, "failed")
        self.assertIsNone(out.fact_check)

    def test_no_fact_check_without_provider_call(self) -> None:
        out = sanitize_research(
            ResearchResult(
                status="completed",
                query="q",
                sources=[
                    ResearchSource(
                        title="T",
                        url="https://example.com",
                        publisher="example.com",
                        retrieved_at=utc_now_iso(),
                    )
                ],
                fact_check=ResearchFactCheck(
                    claim="c", verdict="supported", confidence=0.9
                ),
            ),
            provider_called=False,
        )
        self.assertNotEqual(out.status, "completed")
        self.assertIsNone(out.fact_check)

    def test_failed_and_unavailable_force_null_fact_check(self) -> None:
        for status in ("failed", "unavailable"):
            out = sanitize_research(
                ResearchResult(
                    status=status,
                    query="q",
                    fact_check=ResearchFactCheck(
                        claim="c", verdict="supported", confidence=0.9
                    ),
                    sources=[
                        ResearchSource(
                            title="T",
                            url="https://example.com",
                            publisher=None,
                            retrieved_at=utc_now_iso(),
                        )
                    ],
                ),
                provider_called=True,
            )
            self.assertEqual(out.status, status)
            self.assertIsNone(out.fact_check)
            self.assertEqual(out.sources, [])

    def test_query_is_clamped(self) -> None:
        long_q = "x" * (MAX_RESEARCH_QUERY_LENGTH + 50)
        out = sanitize_research(
            ResearchResult(
                status="failed",
                query=long_q,
                sources=[],
            ),
            provider_called=True,
        )
        self.assertEqual(len(out.query or ""), MAX_RESEARCH_QUERY_LENGTH)
        self.assertEqual(len(clamp_research_query(long_q)), MAX_RESEARCH_QUERY_LENGTH)


class AnthropicProviderBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_api_key_unavailable(self) -> None:
        provider = AnthropicResearchProvider(api_key="")
        turn = await provider.research("Fact-check this: water boils at 100C.")
        self.assertEqual(turn.research.status, "unavailable")
        self.assertIsNone(turn.research.fact_check)

    async def test_timeout_returns_failed(self) -> None:
        provider = AnthropicResearchProvider(api_key="test-key", timeout=0.01)  # pragma: allowlist secret

        async def _boom(**_kwargs):
            raise httpx.TimeoutException("timed out")

        with patch.object(provider, "_messages_create", side_effect=_boom):
            turn = await provider.research("Search the web for boiling point of water.")
        self.assertEqual(turn.research.status, "failed")
        self.assertIsNone(turn.research.fact_check)
        self.assertIn("timed out", (turn.research.uncertainty or "").lower())

    async def test_malformed_response_returns_failed(self) -> None:
        provider = AnthropicResearchProvider(api_key="test-key")  # pragma: allowlist secret

        async def _bad(**_kwargs):
            return "not-a-dict"

        with patch.object(provider, "_messages_create", side_effect=_bad):
            turn = await provider.research("Find sources for this claim.")
        self.assertEqual(turn.research.status, "failed")
        self.assertIsNone(turn.research.fact_check)

    async def test_zero_valid_sources_returns_failed(self) -> None:
        provider = AnthropicResearchProvider(api_key="test-key")  # pragma: allowlist secret

        async def _empty_sources(**_kwargs):
            return {
                "content": [
                    {
                        "type": "web_search_tool_result",
                        "content": [
                            {
                                "type": "web_search_result",
                                "title": "Bad",
                                "url": "javascript:alert(1)",
                            }
                        ],
                    },
                    {"type": "text", "text": "No good sources."},
                ]
            }

        with patch.object(provider, "_messages_create", side_effect=_empty_sources):
            turn = await provider.research("Fact-check this: anything.")
        self.assertEqual(turn.research.status, "failed")
        self.assertEqual(turn.research.sources, [])
        self.assertIsNone(turn.research.fact_check)

    async def test_valid_sources_may_complete_and_cap_at_five(self) -> None:
        provider = AnthropicResearchProvider(api_key="test-key")  # pragma: allowlist secret

        async def _many(**_kwargs):
            results = [
                {
                    "type": "web_search_result",
                    "title": f"S{i}",
                    "url": f"https://example.com/{i}",
                }
                for i in range(8)
            ]
            return {
                "content": [
                    {"type": "web_search_tool_result", "content": results},
                    {
                        "type": "text",
                        "text": "Summary.\nFACT_CHECK: supported| 0.7| claim",
                    },
                ]
            }

        with patch.object(provider, "_messages_create", side_effect=_many):
            turn = await provider.research("Fact-check this: claim.")
        self.assertEqual(turn.research.status, "completed")
        self.assertEqual(len(turn.research.sources), MAX_PUBLIC_SOURCES)
        self.assertIsNotNone(turn.research.fact_check)


class BrainstormChatResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        # Chat tests use the AUTH_MODE=dev bypass + "Bearer test". Force that
        # even when the developer .env has AUTH_MODE=self.
        self._prev_auth_mode = os.environ.get("AUTH_MODE")
        os.environ["AUTH_MODE"] = "dev"
        self._prev_app_env = os.environ.get("APP_ENV")
        os.environ["APP_ENV"] = "test"
        from shared.profile_schema import empty_profile

        self._extra_patches = [
            patch("shared.db.get_conversation", new_callable=AsyncMock, return_value=None),
            patch(
                "shared.db.load_messages_with_seq",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "shared.db.set_conversation_title_if_empty", new_callable=AsyncMock
            ),
            patch(
                "main.get_profile",
                new_callable=AsyncMock,
                return_value=empty_profile(
                    "00000000-0000-4000-8000-000000000001"
                ),
            ),
        ]
        for p in self._extra_patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self) -> None:
        clear_research_provider_override()
        if self._prev_auth_mode is None:
            os.environ.pop("AUTH_MODE", None)
        else:
            os.environ["AUTH_MODE"] = self._prev_auth_mode
        if self._prev_app_env is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = self._prev_app_env

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.db.create_conversation", new_callable=AsyncMock)
    @patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[])
    @patch("shared.db.append_message", new_callable=AsyncMock)
    @patch(
        "main._run_model_turn",
        new_callable=AsyncMock,
        return_value=("Just brainstorming with you.", None, None),
    )
    def test_ordinary_brainstorm_returns_research_null(
        self,
        mock_turn: AsyncMock,
        _append: AsyncMock,
        _load: AsyncMock,
        _create: AsyncMock,
        _close: AsyncMock,
        _init: AsyncMock,
    ) -> None:
        fake = FakeResearchProvider(_completed_turn())
        set_research_provider_for_tests(fake)
        prev = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "unittest-placeholder"  # pragma: allowlist secret
        try:
            with TestClient(app) as client:
                resp = client.post(
                    "/chat",
                    json={
                        "transcript": "Please check my reasoning on this twist.",
                        "mode": "brainstorm",
                        "conversation_id": None,
                    },
                    headers={"Authorization": "Bearer test"},
                )
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIsNone(body["research"])
        self.assertIsNone(body["visual_panel"])
        self.assertEqual(fake.calls, [])
        mock_turn.assert_awaited()

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.db.create_conversation", new_callable=AsyncMock)
    @patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[])
    @patch("shared.db.append_message", new_callable=AsyncMock)
    def test_explicit_fact_check_triggers_provider_with_source(
        self,
        _append: AsyncMock,
        _load: AsyncMock,
        _create: AsyncMock,
        _close: AsyncMock,
        _init: AsyncMock,
    ) -> None:
        fake = FakeResearchProvider(_completed_turn())
        set_research_provider_for_tests(fake)
        prev = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "unittest-placeholder"  # pragma: allowlist secret
        try:
            with TestClient(app) as client:
                resp = client.post(
                    "/chat",
                    json={
                        "transcript": "Please fact-check this: the sky is blue.",
                        "mode": "brainstorm",
                        "conversation_id": None,
                    },
                    headers={"Authorization": "Bearer test"},
                )
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(fake.calls), 1)
        research = body["research"]
        self.assertIsNotNone(research)
        self.assertEqual(research["status"], "completed")
        self.assertGreaterEqual(len(research["sources"]), 1)
        self.assertNotIn("snippet", research["sources"][0])
        self.assertIsNotNone(research["fact_check"])
        self.assertIsNone(body["visual_panel"])
        self.assertIsNone(body["pending_action"])

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.db.create_conversation", new_callable=AsyncMock)
    @patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[])
    @patch("shared.db.append_message", new_callable=AsyncMock)
    def test_provider_failure_returns_failed(
        self,
        _append: AsyncMock,
        _load: AsyncMock,
        _create: AsyncMock,
        _close: AsyncMock,
        _init: AsyncMock,
    ) -> None:
        failed = ResearchTurn(
            reply="Search failed.",
            research=sanitize_research(
                ResearchResult(status="failed", query="look this up", sources=[]),
                provider_called=True,
            ),
        )
        set_research_provider_for_tests(FakeResearchProvider(failed))
        prev = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "unittest-placeholder"  # pragma: allowlist secret
        try:
            with TestClient(app) as client:
                resp = client.post(
                    "/chat",
                    json={
                        "transcript": "Look this up: boiling point of water.",
                        "mode": "brainstorm",
                        "conversation_id": None,
                    },
                    headers={"Authorization": "Bearer test"},
                )
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev

        body = resp.json()
        self.assertEqual(body["research"]["status"], "failed")
        self.assertIsNone(body["research"]["fact_check"])

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.db.create_conversation", new_callable=AsyncMock)
    @patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[])
    @patch("shared.db.append_message", new_callable=AsyncMock)
    def test_missing_provider_returns_unavailable(
        self,
        _append: AsyncMock,
        _load: AsyncMock,
        _create: AsyncMock,
        _close: AsyncMock,
        _init: AsyncMock,
    ) -> None:
        set_research_provider_for_tests(None)
        prev = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "unittest-placeholder"  # pragma: allowlist secret
        try:
            with TestClient(app) as client:
                resp = client.post(
                    "/chat",
                    json={
                        "transcript": "Research this: founding of the FDA.",
                        "mode": "brainstorm",
                        "conversation_id": None,
                    },
                    headers={"Authorization": "Bearer test"},
                )
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev

        body = resp.json()
        self.assertEqual(body["research"]["status"], "unavailable")
        self.assertIsNone(body["research"]["fact_check"])

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.db.create_conversation", new_callable=AsyncMock)
    @patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[])
    @patch("shared.db.append_message", new_callable=AsyncMock)
    @patch(
        "main._run_model_turn",
        new_callable=AsyncMock,
        return_value=("Fitness reply.", None, None),
    )
    def test_other_modes_unchanged_no_research(
        self,
        mock_turn: AsyncMock,
        _append: AsyncMock,
        _load: AsyncMock,
        _create: AsyncMock,
        _close: AsyncMock,
        _init: AsyncMock,
    ) -> None:
        fake = FakeResearchProvider(_completed_turn())
        set_research_provider_for_tests(fake)
        prev = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "unittest-placeholder"  # pragma: allowlist secret
        try:
            with TestClient(app) as client:
                resp = client.post(
                    "/chat",
                    json={
                        "transcript": "Please fact-check this: my form on bench press.",
                        "mode": "fitness",
                        "conversation_id": None,
                    },
                    headers={"Authorization": "Bearer test"},
                )
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["research"])
        self.assertEqual(fake.calls, [])
        mock_turn.assert_awaited()

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.db.create_conversation", new_callable=AsyncMock)
    @patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[])
    @patch("shared.db.append_message", new_callable=AsyncMock)
    def test_provider_cannot_inject_unsupported_status(
        self,
        _append: AsyncMock,
        _load: AsyncMock,
        _create: AsyncMock,
        _close: AsyncMock,
        _init: AsyncMock,
    ) -> None:
        injected = ResearchTurn(
            reply="still searching",
            research=ResearchResult(
                status="running",
                query="check whether this is true",
                sources=[
                    ResearchSource(
                        title="T",
                        url="https://example.com",
                        publisher="example.com",
                        retrieved_at=utc_now_iso(),
                    )
                ],
                fact_check=ResearchFactCheck(
                    claim="c", verdict="supported", confidence=0.9
                ),
            ),
        )
        set_research_provider_for_tests(FakeResearchProvider(injected))
        prev = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "unittest-placeholder"  # pragma: allowlist secret
        try:
            with TestClient(app) as client:
                resp = client.post(
                    "/chat",
                    json={
                        "transcript": "Check whether this is true.",
                        "mode": "brainstorm",
                        "conversation_id": None,
                    },
                    headers={"Authorization": "Bearer test"},
                )
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev

        body = resp.json()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(body["research"]["status"], "failed")
        self.assertIsNone(body["research"]["fact_check"])
        self.assertNotIn(body["research"]["status"], ("running", "completed"))


if __name__ == "__main__":
    unittest.main()
