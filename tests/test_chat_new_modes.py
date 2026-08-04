"""Slice 1B: /chat accepts brainstorm and mail_calendar mode keys.

Run:  python -m unittest tests.test_chat_new_modes -v
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import MODE_REGISTRY, PUBLIC_MODE_IDS, app


class ChatNewModesTests(unittest.TestCase):
    def test_new_keys_are_in_registry_and_public_catalog(self) -> None:
        for mode in ("brainstorm", "mail_calendar"):
            self.assertIn(mode, MODE_REGISTRY)
            self.assertIn(mode, PUBLIC_MODE_IDS)

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.db.create_conversation", new_callable=AsyncMock)
    @patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[])
    @patch("shared.db.append_message", new_callable=AsyncMock)
    @patch(
        "main._run_model_turn",
        new_callable=AsyncMock,
        return_value=("Shell reply.", None),
    )
    def test_chat_accepts_brainstorm_and_mail_calendar(
        self,
        _turn: AsyncMock,
        _append: AsyncMock,
        _load: AsyncMock,
        _create: AsyncMock,
        _close: AsyncMock,
        _init: AsyncMock,
    ) -> None:
        headers = {"Authorization": "Bearer test"}
        prev = os.environ.get("ANTHROPIC_API_KEY")
        # Placeholder only — never a real credential (detect-secrets).
        os.environ["ANTHROPIC_API_KEY"] = "unittest-placeholder"  # pragma: allowlist secret
        try:
            with TestClient(app) as client:
                for mode in ("brainstorm", "mail_calendar"):
                    resp = client.post(
                        "/chat",
                        json={
                            "transcript": "Hello.",
                            "mode": mode,
                            "conversation_id": None,
                        },
                        headers=headers,
                    )
                    self.assertEqual(resp.status_code, 200, resp.text)
                    body = resp.json()
                    self.assertEqual(body["mode"], mode)
                    self.assertIsNone(body["research"])
                    self.assertIsNone(body["pending_action"])
                    self.assertIsNone(body["visual_panel"])
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    def test_chat_rejects_retired_health(
        self,
        _close: AsyncMock,
        _init: AsyncMock,
    ) -> None:
        with TestClient(app) as client:
            resp = client.post(
                "/chat",
                json={
                    "transcript": "Hello.",
                    "mode": "health",
                    "conversation_id": None,
                },
                headers={"Authorization": "Bearer test"},
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unsupported mode", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
