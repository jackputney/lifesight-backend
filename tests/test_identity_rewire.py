"""Identity rewire — FK inventory + chat ownership isolation (mocked DB).

Run:  python -m unittest tests.test_identity_rewire -v
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from shared.local_auth.store import use_memory_store

REPO = Path(__file__).resolve().parents[1]
MIGRATION_007 = REPO / "migrations" / "007_identity_rewire_public_users.sql"

# Tables that must be rewired off auth.users (from live inventory + migrations).
EXPECTED_REWIRE_TABLES = frozenset({
    "action_log",
    "conversations",
    "daily_nutrition_targets",
    "devices",
    "food_entries",
    "health_entries",
    "health_metrics",
    "health_plans",
    "manuscripts",
    "memories",
    "oauth_credentials",
    "pending_actions",
    "personal_records",
    "reminders",
    "sync_state",
    "wearable_connections",
    "workout_plans",
    "workout_sessions",
    "writing_documents",
})


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "self",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "",  # chat should 500 for missing key only after ownership works
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class Migration007Tests(unittest.TestCase):
    def test_migration_file_documents_rollback_and_rewires_expected_tables(self):
        text = MIGRATION_007.read_text(encoding="utf-8")
        self.assertIn("ROLLBACK NOTES", text)
        self.assertIn("REFERENCES users(id)", text)
        self.assertIn("DROP CONSTRAINT IF EXISTS conversations_user_id_fkey", text)
        self.assertIn("Legacy preserved account", text)
        self.assertIn("00000000-0000-4000-8000-000000000001", text)
        for table in EXPECTED_REWIRE_TABLES:
            self.assertIn(f"{table}_user_id_fkey", text, msg=table)
            self.assertIn(
                f"REFERENCES users(id) ON DELETE CASCADE",
                text,
            )


class ChatOwnershipTests(unittest.TestCase):
    def setUp(self):
        use_memory_store(True)
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._pool_init.start()
        self._pool_close.start()
        self.addCleanup(self._pool_init.stop)
        self.addCleanup(self._pool_close.stop)
        self.addCleanup(lambda: use_memory_store(False))

    def test_chat_rejects_other_users_conversation_id(self):
        from main import app

        with _env():
            with TestClient(app) as client:
                a = client.post(
                    "/auth/register",
                    json={
                        "username": "ownera",
                        "password": "password123",  # pragma: allowlist secret
                        "email": "a@example.com",
                    },
                ).json()
                b = client.post(
                    "/auth/register",
                    json={
                        "username": "ownerb",
                        "password": "password123",  # pragma: allowlist secret
                        "email": "b@example.com",
                    },
                ).json()

                convo = {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "user_id": a["user"]["id"],
                    "mode": "fitness",
                    "summary_text": None,
                    "summary_through_seq": None,
                }

                async def fake_get(cid: str):
                    if cid == convo["id"]:
                        return convo
                    return None

                with patch("shared.db.get_conversation", side_effect=fake_get), patch(
                    "shared.db.create_conversation", new_callable=AsyncMock
                ) as create, patch(
                    "shared.db.load_messages", new_callable=AsyncMock, return_value=[]
                ), patch(
                    "shared.db.load_messages_with_seq",
                    new_callable=AsyncMock,
                    return_value=[],
                ), patch(
                    "shared.db.set_conversation_title_if_empty", new_callable=AsyncMock
                ), patch(
                    "shared.db.append_message", new_callable=AsyncMock
                ), patch(
                    "main.get_profile",
                    new_callable=AsyncMock,
                    return_value=__import__(
                        "shared.profile_schema", fromlist=["empty_profile"]
                    ).empty_profile(a["user"]["id"]),
                ), patch(
                    "main._run_model_turn",
                    new_callable=AsyncMock,
                    return_value=("ok", None, None, []),
                ):
                    # A can continue their conversation (model path mocked).
                    os.environ["ANTHROPIC_API_KEY"] = "test-key"  # pragma: allowlist secret
                    ok = client.post(
                        "/chat",
                        headers={"Authorization": f"Bearer {a['access_token']}"},
                        json={
                            "transcript": "hi",
                            "mode": "fitness",
                            "conversation_id": convo["id"],
                        },
                    )
                    self.assertEqual(ok.status_code, 200, ok.text)
                    create.assert_not_awaited()

                    forbidden = client.post(
                        "/chat",
                        headers={"Authorization": f"Bearer {b['access_token']}"},
                        json={
                            "transcript": "hi",
                            "mode": "fitness",
                            "conversation_id": convo["id"],
                        },
                    )
                    self.assertEqual(forbidden.status_code, 403)


if __name__ == "__main__":
    unittest.main()
