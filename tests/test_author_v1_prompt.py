"""Author V1 prompt/behavior contract (deterministic — no Claude calls).

Covers drafting guidance, fiction/reality boundary, epistemic preservation,
and user prompt override layering for Author mode.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from main import MODE_REGISTRY, MODE_TOOLS, _build_system_prompt
from modes.author import prompt as author_prompt
from shared.epistemic import (
    EPISTEMIC_GROUNDING,
    FEASIBILITY_AND_NON_SYCOPHANCY,
)
from shared.prompt_overrides import format_user_customization_block


class AuthorPromptContractTests(unittest.TestCase):
    def test_direct_drafting_genres_and_return_writing_first(self):
        text = author_prompt.INSTRUCTIONS.lower()
        for needle in (
            "write",
            "rewrite",
            "shorten",
            "expand",
            "continue",
            "tone",
            "audience",
            "email",
            "social",
            "script",
            "article",
            "notes",
            "fiction",
            "return the writing first",
        ):
            self.assertIn(needle, text)

    def test_avoids_unnecessary_praise_preamble_disclaimer(self):
        text = author_prompt.INSTRUCTIONS.lower()
        self.assertIn("praise", text)
        self.assertIn("preambles", text)
        self.assertIn("here is your draft", text)
        self.assertIn("reality disclaimers", text)

    def test_pure_fiction_without_unsolicited_reality_coda(self):
        text = author_prompt.INSTRUCTIONS
        self.assertIn("streetlights are secretly communicating", text)
        self.assertIn("write fiction normally", text.lower())
        self.assertIn("unsolicited reality coda", text.lower())

    def test_real_world_extraordinary_claim_still_grounded(self):
        text = author_prompt.INSTRUCTIONS
        self.assertIn("streetlights really are communicating about me", text)
        self.assertIn("shared epistemic grounding applies", text.lower())
        composed = author_prompt.SYSTEM_PROMPT
        self.assertIn(EPISTEMIC_GROUNDING, composed)
        self.assertIn(FEASIBILITY_AND_NON_SYCOPHANCY, composed)

    def test_epistemic_and_feasibility_not_weakened(self):
        built = _build_system_prompt("author")
        self.assertIn(EPISTEMIC_GROUNDING, built)
        self.assertIn(FEASIBILITY_AND_NON_SYCOPHANCY, built)
        self.assertLess(
            built.index(EPISTEMIC_GROUNDING),
            built.index(FEASIBILITY_AND_NON_SYCOPHANCY),
        )
        self.assertIn("never weakened", author_prompt.INSTRUCTIONS.lower())

    def test_manuscript_model_preserved(self):
        text = author_prompt.INSTRUCTIONS.lower()
        self.assertIn("manuscripts", text)
        self.assertIn("chapters", text)
        self.assertIn("scenes", text)
        self.assertIn("google docs is not used", text)
        self.assertIn("create_pending_action", str(MODE_TOOLS["author"]))

    def test_user_prompt_override_reaches_author(self):
        block = format_user_customization_block(
            global_instructions=None,
            mode_instructions="Prefer concise noir dialogue.",
        )
        built = _build_system_prompt("author", user_customization_block=block)
        self.assertIn("Prefer concise noir dialogue.", built)
        # Customization is subordinate — epistemic precedes it.
        self.assertLess(
            built.index(EPISTEMIC_GROUNDING),
            built.index("Prefer concise noir dialogue."),
        )

    def test_registry_uses_refined_author_prompt(self):
        self.assertIs(MODE_REGISTRY["author"], author_prompt.SYSTEM_PROMPT)


class AuthorHistoryResumeIntactTests(unittest.TestCase):
    """Conversation id / history plumbing is unchanged for Author."""

    def test_chat_request_still_accepts_conversation_id(self):
        from main import ChatRequest

        req = ChatRequest(
            transcript="rewrite this shorter",
            mode="author",
            conversation_id="11111111-1111-4111-8111-111111111111",
        )
        self.assertEqual(req.mode, "author")
        self.assertEqual(
            req.conversation_id, "11111111-1111-4111-8111-111111111111"
        )


if __name__ == "__main__":
    unittest.main()
