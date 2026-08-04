"""Brainstorm research provider wiring — fake provider only (no live paid search).

Run:  python -m unittest tests.test_brainstorm_research -v
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app
from shared.research import (
    ResearchFactCheck,
    ResearchResult,
    ResearchSource,
    ResearchTurn,
    clear_research_provider_override,
    sanitize_research,
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


def _completed_turn(query: str = "fact-check: the sky is blue") -> ResearchTurn:
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

    def test_explicit_phrases_want_research(self) -> None:
        self.assertTrue(wants_research("Please fact-check that the FDA was founded in 1906."))
        self.assertTrue(wants_research("Can you research when the FDA was founded?"))
        self.assertTrue(wants_research("Verify this claim for me."))
        self.assertTrue(wants_research("Look up the founding year of the FDA."))
        self.assertTrue(wants_research("Check whether water boils at 100C at sea level."))


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


class BrainstormChatResearchTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_research_provider_override()

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.db.create_conversation", new_callable=AsyncMock)
    @patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[])
    @patch("shared.db.append_message", new_callable=AsyncMock)
    @patch(
        "main._run_model_turn",
        new_callable=AsyncMock,
        return_value=("Just brainstorming with you.", None),
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
                        "transcript": "What if we reverse the plot twist?",
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
                        "transcript": "Please fact-check: the sky is blue.",
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
                ResearchResult(status="failed", query="look up X", sources=[]),
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
                        "transcript": "Look up the boiling point of water.",
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
                        "transcript": "Research the founding of the FDA.",
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
        return_value=("Fitness reply.", None),
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
                        "transcript": "Please fact-check my form on bench press.",
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
                status="running",  # not a public status
                query="check this",
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
        # Provider returns unsanitized payload; chat path must sanitize.
        # Simulate a careless provider by wrapping sanitize at the edge in chat:
        # we sanitize in AnthropicResearchProvider / Fake should sanitize before return.
        # This test asserts sanitize_research itself rejects injection; chat uses
        # provider output as-is only if already sanitized — harden chat to sanitize.
        set_research_provider_for_tests(FakeResearchProvider(injected))
        prev = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "unittest-placeholder"  # pragma: allowlist secret
        try:
            with TestClient(app) as client:
                resp = client.post(
                    "/chat",
                    json={
                        "transcript": "Check this claim for me.",
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
