"""Epistemic grounding — shared policy composition + scenario steering.

Deterministic checks: policy text is composed into every mode prompt and
contains steering language for representative failure modes. These do not
call Claude; they prove the server-side contract the model is instructed with.

Also regresses that public modes / tools / client-action imports still load.

Run:  python -m unittest tests.test_epistemic_grounding -v
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import MODE_REGISTRY, MODE_TOOLS, PUBLIC_MODE_IDS, _build_system_prompt, app
from modes.author import prompt as author_prompt
from modes.brainstorm import prompt as brainstorm_prompt
from modes.diet import prompt as diet_prompt
from modes.fitness import prompt as fitness_prompt
from modes.mail_calendar import prompt as mail_calendar_prompt
from shared.epistemic import EPISTEMIC_GROUNDING, compose_system_prompt
from shared.identity import IDENTITY


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "dev",
        "APP_ENV": "test",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "unittest-placeholder",  # pragma: allowlist secret
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class ComposeArchitectureTests(unittest.TestCase):
    def test_compose_order_identity_then_epistemic_then_mode(self):
        prompt = compose_system_prompt("MODE BODY")
        self.assertTrue(prompt.startswith(IDENTITY))
        id_end = prompt.index(IDENTITY) + len(IDENTITY)
        ep_start = prompt.index(EPISTEMIC_GROUNDING)
        mode_start = prompt.index("MODE BODY")
        self.assertLess(id_end, ep_start)
        self.assertLess(ep_start, mode_start)

    def test_all_registry_modes_include_shared_epistemic_layer(self):
        for mode, prompt in MODE_REGISTRY.items():
            with self.subTest(mode=mode):
                self.assertIn(IDENTITY, prompt)
                self.assertIn(EPISTEMIC_GROUNDING, prompt)

    def test_runtime_build_preserves_epistemic_layer(self):
        built = _build_system_prompt("fitness")
        self.assertIn(EPISTEMIC_GROUNDING, built)
        self.assertIn("Today's date is", built)

    def test_policy_not_exposed_via_modes_endpoint(self):
        with _env():
            with patch("shared.db.init_pool", new_callable=AsyncMock), patch(
                "shared.db.close_pool", new_callable=AsyncMock
            ):
                with TestClient(app) as client:
                    resp = client.get("/modes")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        blob = str(body)
        self.assertNotIn("Epistemic grounding", blob)
        self.assertNotIn(EPISTEMIC_GROUNDING[:40], blob)
        self.assertEqual(body["modes"], list(PUBLIC_MODE_IDS))


class ScenarioSteeringTests(unittest.TestCase):
    """Representative user claims → required policy clauses in the system prompt."""

    def _policy(self) -> str:
        return EPISTEMIC_GROUNDING

    def test_unsupported_surveillance_not_affirmed(self):
        # "The same car drove past twice → government is monitoring me."
        policy = self._policy().lower()
        self.assertIn("watched", policy)
        self.assertIn("surveillance", policy)
        self.assertIn("do not affirm unsupported claims", policy)
        self.assertIn("coincidences", policy)
        self.assertIn("ordinary plausible explanations", policy)

    def test_hidden_messages_observation_vs_interpretation(self):
        # "TV glitched when I thought about my ex → hidden signal?"
        policy = self._policy().lower()
        self.assertIn("hidden signals", policy)
        self.assertIn("separate that from their interpretation", policy)
        self.assertIn("observed fact", policy)
        self.assertIn("speculation", policy)

    def test_grandiosity_not_validated_without_evidence(self):
        # "I discovered a physics law scientists are hiding because only I understand it."
        policy = self._policy().lower()
        self.assertIn("uniquely chosen", policy)
        self.assertIn("extraordinary powers", policy)
        self.assertIn("hidden conspiracy", policy)
        self.assertIn("do not invent", policy)
        self.assertIn("agree only when evidence", policy)

    def test_coincidence_causal_gap(self):
        policy = self._policy().lower()
        self.assertIn("causal gaps", policy)
        self.assertIn("coincidence or correlation", policy)
        self.assertIn("alternative explanations", policy)

    def test_balance_not_contrarian_on_mundane_claims(self):
        policy = self._policy().lower()
        self.assertIn("do not challenge mundane, well-supported statements", policy)
        self.assertIn("stay useful and conversational", policy)

    def test_fiction_reality_boundary(self):
        policy = self._policy().lower()
        self.assertIn("fiction and reality", policy)
        self.assertIn("clearly fictional work", policy)

    def test_model_self_awareness_limits(self):
        policy = self._policy().lower()
        self.assertIn("never imply you independently observed", policy)
        self.assertIn("authorized tool or data source", policy)
        self.assertIn("unverifiability as proof", policy)

    def test_user_report_is_not_independently_established_fact(self):
        """User report != independently established interpretation."""
        policy = self._policy().lower()
        # Must not treat user statements as automatic proof of interpretation.
        self.assertNotIn("explicit user-provided facts", policy)
        self.assertIn(
            "not automatically proof that the user's interpretation",
            policy,
        )
        self.assertIn('the user reports x', policy)
        self.assertIn("independently established", policy)
        self.assertIn(
            "does not by itself verify an extraordinary interpretation",
            policy,
        )
        # Especially for extraordinary categories.
        for phrase in (
            "surveillance",
            "hidden messages",
            "conspiracies",
            "supernatural",
            "grandiose",
            "coincidence",
        ):
            self.assertIn(phrase, policy)

    def test_observation_vs_explanation_car_example(self):
        # Report: saw same person/car several times. Not: they are surveilling you.
        policy = self._policy().lower()
        self.assertIn("same car outside three times", policy)
        self.assertIn("government is monitoring me", policy)
        self.assertIn("does not establish", policy)
        self.assertIn("acknowledge the report", policy)

    def test_internal_experience_vs_external_fact_guidance(self):
        # Hearing/experiencing something ≠ verified external sender/purpose.
        policy = self._policy().lower()
        self.assertIn("hidden messages or signals", policy)
        self.assertIn("user-reported claim", policy)
        self.assertIn("what the user reports or experienced", policy)
        self.assertIn("not automatically proof", policy)

    def test_repeated_confidence_does_not_raise_epistemic_status(self):
        policy = self._policy().lower()
        self.assertIn("sincere, confident, repeated, or detailed", policy)
        self.assertIn(
            "does not by itself verify an extraordinary interpretation",
            policy,
        )
        self.assertIn(
            "never because the user sounds confident or repeats a claim",
            policy,
        )


class ModeSpecificSteeringTests(unittest.TestCase):
    def test_author_allows_fiction_with_reality_boundary(self):
        text = author_prompt.SYSTEM_PROMPT.lower()
        self.assertIn("creative fiction is encouraged", text)
        self.assertIn("fiction/reality boundary", text)
        self.assertIn(EPISTEMIC_GROUNDING.lower().splitlines()[0], text)

    def test_brainstorm_labels_speculation_and_ranks_plausibility(self):
        text = brainstorm_prompt.INSTRUCTIONS.lower()
        self.assertIn("label speculation as speculation", text)
        self.assertIn("rank plausibility", text)
        self.assertIn("discovered truth", text)
        # Informal unrelated coaching lines must not remain.
        self.assertNotIn("workout videos", text)
        self.assertNotIn("ask their age", text)

    def test_fitness_evidence_oriented_physiology(self):
        text = fitness_prompt.INSTRUCTIONS.lower()
        self.assertIn("evidence-oriented", text)
        self.assertIn("hormonal states", text)
        self.assertIn("weak evidence", text)

    def test_diet_distinguishes_evidence_from_anecdote(self):
        text = diet_prompt.INSTRUCTIONS.lower()
        self.assertIn("established nutrition evidence", text)
        self.assertIn("mechanistic speculation", text)
        self.assertIn("unsupported medical explanations", text)

    def test_mail_calendar_no_invented_connected_data(self):
        text = mail_calendar_prompt.INSTRUCTIONS.lower()
        self.assertIn("never invent inbox contents", text)
        self.assertIn("absence of retrieved information", text)
        self.assertIn("hidden message", text)


class RegressionSmokeTests(unittest.TestCase):
    """Shared policy must not break navigation, panels, modes, or chat wiring."""

    def test_public_mode_order_unchanged(self):
        self.assertEqual(
            list(PUBLIC_MODE_IDS),
            ["fitness", "diet", "author", "brainstorm", "mail_calendar"],
        )

    def test_fitness_still_exposes_exercise_panel_tool(self):
        names = [t["name"] for t in MODE_TOOLS["fitness"]]
        self.assertIn("present_exercise_panel", names)
        self.assertIn("update_personal_context", names)

    def test_author_still_exposes_confirm_gate_tool(self):
        names = [t["name"] for t in MODE_TOOLS["author"]]
        self.assertIn("create_pending_action", names)
        self.assertIn("update_personal_context", names)

    def test_brainstorm_and_mail_expose_personal_context_only(self):
        self.assertEqual(
            [t["name"] for t in MODE_TOOLS["brainstorm"]],
            ["update_personal_context"],
        )
        self.assertEqual(
            [t["name"] for t in MODE_TOOLS["mail_calendar"]],
            ["update_personal_context"],
        )

    def test_navigate_client_action_still_works(self):
        from shared.client_actions import parse_navigate_command

        match = parse_navigate_command("Open fitness.")
        self.assertIsNotNone(match)
        self.assertEqual(match.target, "fitness")

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.db.create_conversation", new_callable=AsyncMock)
    @patch("shared.db.get_conversation", new_callable=AsyncMock, return_value=None)
    @patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[])
    @patch("shared.db.load_messages_with_seq", new_callable=AsyncMock, return_value=[])
    @patch("shared.db.append_message", new_callable=AsyncMock)
    @patch("shared.db.set_conversation_title_if_empty", new_callable=AsyncMock)
    @patch("main.get_profile", new_callable=AsyncMock)
    @patch(
        "main._run_model_turn",
        new_callable=AsyncMock,
        return_value=(
            "Sure — progressive overload means adding stress over time.",
            None,
            None,
            [],
        ),
    )
    def test_chat_ordinary_turn_still_invokes_model(
        self,
        turn: AsyncMock,
        mock_profile: AsyncMock,
        _title: AsyncMock,
        _append: AsyncMock,
        _load_seq: AsyncMock,
        _load: AsyncMock,
        _get: AsyncMock,
        mock_create: AsyncMock,
        _close: AsyncMock,
        _init: AsyncMock,
    ):
        from shared.profile_schema import empty_profile

        mock_profile.return_value = empty_profile(
            "00000000-0000-4000-8000-000000000001"
        )
        mock_create.return_value = "00000000-0000-4000-8000-000000000099"
        with _env():
            with TestClient(app) as client:
                resp = client.post(
                    "/chat",
                    json={
                        "transcript": "What is progressive overload?",
                        "mode": "fitness",
                        "conversation_id": None,
                    },
                    headers={"Authorization": "Bearer test"},
                )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("reply", data)
        self.assertIsNone(data.get("pending_action"))
        self.assertEqual(data.get("client_actions"), [])
        turn.assert_called_once()
        kwargs = turn.await_args.kwargs
        self.assertIn("system_prompt", kwargs)
        self.assertIn(EPISTEMIC_GROUNDING, kwargs["system_prompt"])


if __name__ == "__main__":
    unittest.main()
