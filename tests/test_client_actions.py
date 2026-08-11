"""Client actions V1 — navigate allowlist + /chat wire shape.

Run:  python -m unittest tests.test_client_actions -v
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import ChatResponse, app
from shared.client_actions import (
    ALLOWED_NAVIGATE_TARGETS,
    NavigateAction,
    parse_navigate_command,
)


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


class ParseNavigateCommandTests(unittest.TestCase):
    def test_open_fitness(self):
        match = parse_navigate_command("Open fitness.")
        self.assertIsNotNone(match)
        self.assertEqual(match.target, "fitness")
        self.assertIsNone(match.blocked_alias)

    def test_take_me_to_author(self):
        match = parse_navigate_command("Take me to Author.")
        self.assertIsNotNone(match)
        self.assertEqual(match.target, "author")

    def test_go_to_settings(self):
        match = parse_navigate_command("Go to settings.")
        self.assertIsNotNone(match)
        self.assertEqual(match.target, "settings")

    def test_open_brainstorm_and_mail_aliases(self):
        self.assertEqual(parse_navigate_command("Open brainstorm").target, "brainstorm")
        self.assertEqual(parse_navigate_command("Open mail").target, "mail_calendar")
        self.assertEqual(parse_navigate_command("Go to calendar").target, "mail_calendar")
        self.assertEqual(parse_navigate_command("Open home").target, "home")
        self.assertEqual(parse_navigate_command("Open diet").target, "diet")

    def test_open_health_is_blocked_not_navigated(self):
        match = parse_navigate_command("Open health.")
        self.assertIsNotNone(match)
        self.assertIsNone(match.target)
        self.assertEqual(match.blocked_alias, "health")

    def test_open_jarvis_is_blocked_not_navigated(self):
        match = parse_navigate_command("Open Jarvis.")
        self.assertIsNotNone(match)
        self.assertIsNone(match.target)
        self.assertEqual(match.blocked_alias, "jarvis")

    def test_ordinary_prompt_is_not_a_command(self):
        self.assertIsNone(
            parse_navigate_command("What does progressive overload mean?")
        )
        self.assertIsNone(parse_navigate_command("Explain creatine."))
        self.assertIsNone(parse_navigate_command("Open my Author project please"))

    def test_allowlist_excludes_health_and_jarvis(self):
        self.assertNotIn("health", ALLOWED_NAVIGATE_TARGETS)
        self.assertNotIn("jarvis", ALLOWED_NAVIGATE_TARGETS)


class ChatResponseClientActionsShapeTests(unittest.TestCase):
    def test_client_actions_default_to_empty_array(self):
        payload = ChatResponse(
            reply="Hello.",
            mode="fitness",
            conversation_id="00000000-0000-4000-8000-000000000099",
        ).model_dump(mode="json")
        self.assertIn("client_actions", payload)
        self.assertEqual(payload["client_actions"], [])
        self.assertIsInstance(payload["client_actions"], list)

    def test_navigate_action_wire_shape(self):
        payload = ChatResponse(
            reply="Opening Fitness.",
            mode="diet",
            conversation_id="00000000-0000-4000-8000-000000000099",
            client_actions=[NavigateAction(type="navigate", target="fitness")],
        ).model_dump(mode="json")
        self.assertEqual(
            payload["client_actions"],
            [{"type": "navigate", "target": "fitness"}],
        )
        # Frozen spoken field remains `reply` (not assistant_text).
        self.assertEqual(payload["reply"], "Opening Fitness.")
        self.assertNotIn("assistant_text", payload)


class ChatClientActionsRouteTests(unittest.TestCase):
    def setUp(self):
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._create = patch("shared.db.create_conversation", new_callable=AsyncMock)
        self._get = patch("shared.db.get_conversation", new_callable=AsyncMock, return_value=None)
        self._load = patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[])
        self._append = patch("shared.db.append_message", new_callable=AsyncMock)
        self._pool_init.start()
        self._pool_close.start()
        self._create.start()
        self._get.start()
        self._load.start()
        self._append.start()
        self.addCleanup(self._pool_init.stop)
        self.addCleanup(self._pool_close.stop)
        self.addCleanup(self._create.stop)
        self.addCleanup(self._get.stop)
        self.addCleanup(self._load.stop)
        self.addCleanup(self._append.stop)

    def _post(self, transcript: str, *, mode: str = "fitness"):
        with _env():
            with TestClient(app) as client:
                return client.post(
                    "/chat",
                    json={
                        "transcript": transcript,
                        "mode": mode,
                        "conversation_id": None,
                    },
                    headers={"Authorization": "Bearer test"},
                )

    def test_open_fitness_returns_navigate_action(self):
        with patch("main._run_model_turn", new_callable=AsyncMock) as turn:
            resp = self._post("Open fitness.")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["reply"], "Opening Fitness.")
        self.assertEqual(
            body["client_actions"],
            [{"type": "navigate", "target": "fitness"}],
        )
        self.assertIsNone(body["pending_action"])
        self.assertIsNone(body["research"])
        self.assertIsNone(body["visual_panel"])
        turn.assert_not_called()

    def test_take_me_to_author_from_fitness_mode(self):
        with patch("main._run_model_turn", new_callable=AsyncMock) as turn:
            resp = self._post("Take me to Author.", mode="fitness")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["mode"], "fitness")  # request mode echoed; action navigates
        self.assertEqual(body["reply"], "Opening Author.")
        self.assertEqual(
            body["client_actions"],
            [{"type": "navigate", "target": "author"}],
        )
        turn.assert_not_called()

    def test_go_to_settings(self):
        resp = self._post("Go to settings.")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json()["client_actions"],
            [{"type": "navigate", "target": "settings"}],
        )

    def test_open_health_does_not_navigate(self):
        with patch("main._run_model_turn", new_callable=AsyncMock) as turn:
            resp = self._post("Open health.")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["client_actions"], [])
        self.assertNotIn("health", str(body["client_actions"]))
        self.assertIn("Health", body["reply"])
        turn.assert_not_called()

    def test_open_jarvis_does_not_navigate(self):
        with patch("main._run_model_turn", new_callable=AsyncMock) as turn:
            resp = self._post("Open Jarvis.")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["client_actions"], [])
        self.assertIn("Jarvis", body["reply"])
        turn.assert_not_called()

    def test_ordinary_prompt_keeps_empty_client_actions_and_calls_model(self):
        with patch(
            "main._run_model_turn",
            new_callable=AsyncMock,
            return_value=("Progressive overload means gradually increasing demand.", None),
        ) as turn:
            resp = self._post("What does progressive overload mean?")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["client_actions"], [])
        self.assertIn("Progressive overload", body["reply"])
        turn.assert_called_once()

    def test_navigate_works_without_anthropic_key(self):
        with _env(ANTHROPIC_API_KEY=""):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with TestClient(app) as client:
                resp = client.post(
                    "/chat",
                    json={
                        "transcript": "Open brainstorm",
                        "mode": "diet",
                        "conversation_id": None,
                    },
                    headers={"Authorization": "Bearer test"},
                )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json()["client_actions"],
            [{"type": "navigate", "target": "brainstorm"}],
        )


if __name__ == "__main__":
    unittest.main()
