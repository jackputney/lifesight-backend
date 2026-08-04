"""Slice 1A: ChatResponse research field compatibility.

Run:  python -m unittest tests.test_chat_response_research -v
"""

from __future__ import annotations

import unittest

from main import ChatResponse
from shared.research import ResearchFactCheck, ResearchResult, ResearchSource


class ChatResponseResearchTests(unittest.TestCase):
    def test_default_research_is_null_in_wire_payload(self) -> None:
        payload = ChatResponse(
            reply="Hello.",
            mode="fitness",
            conversation_id="00000000-0000-4000-8000-000000000099",
            pending_action=None,
            visual_panel=None,
            research=None,
        ).model_dump(mode="json")

        self.assertIn("research", payload)
        self.assertIsNone(payload["research"])
        self.assertIn("visual_panel", payload)
        self.assertIsNone(payload["visual_panel"])

    def test_omitted_research_defaults_to_null(self) -> None:
        """Existing constructors that omit research stay compatible."""
        payload = ChatResponse(
            reply="Hello.",
            mode="author",
            conversation_id="00000000-0000-4000-8000-000000000099",
        ).model_dump(mode="json")
        self.assertIsNone(payload["research"])

    def test_research_shape_round_trip(self) -> None:
        research = ResearchResult(
            status="completed",
            query="when was the FDA founded?",
            summary="Sources point to 1906 lineage.",
            uncertainty="Agency rename vs act date.",
            sources=[
                ResearchSource(
                    title="History",
                    url="https://example.com/fda",
                    publisher="Example",
                    retrieved_at="2026-08-04T20:00:00Z",
                )
            ],
            fact_check=ResearchFactCheck(
                claim="The FDA was founded in 1906.",
                verdict="supported",
                confidence=0.72,
            ),
        )
        payload = ChatResponse(
            reply="Here is what I found.",
            mode="brainstorm",
            conversation_id="00000000-0000-4000-8000-000000000099",
            research=research,
        ).model_dump(mode="json")

        self.assertEqual(payload["research"]["status"], "completed")
        self.assertEqual(len(payload["research"]["sources"]), 1)
        self.assertNotIn("snippet", payload["research"]["sources"][0])
        self.assertEqual(payload["research"]["fact_check"]["verdict"], "supported")


if __name__ == "__main__":
    unittest.main()
