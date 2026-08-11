"""Author create actions via /chat — server mutation + result client_actions.

Run:  python -m unittest tests.test_author_actions -v
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import ChatResponse, app
from shared.author_actions import parse_author_command
from shared.author_persistence import store as author_store
from shared.client_actions import (
    AuthorDocumentCreatedAction,
    AuthorProjectCreatedAction,
    NavigateAction,
)
from shared.local_auth.store import use_memory_store


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "self",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "unittest-placeholder",  # pragma: allowlist secret
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class ParseAuthorCommandTests(unittest.TestCase):
    def test_create_project_phrases(self):
        cmd = parse_author_command('Create an Author project called My Novel.')
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.kind, "create_project")
        self.assertEqual(cmd.title, "My Novel")

        cmd2 = parse_author_command('Please create a project named "Summer Book"')
        self.assertEqual(cmd2.kind, "create_project")
        self.assertEqual(cmd2.title, "Summer Book")

    def test_create_document_with_and_without_project(self):
        with_proj = parse_author_command(
            "Create a document called Chapter One in My Novel."
        )
        self.assertEqual(with_proj.kind, "create_document")
        self.assertEqual(with_proj.title, "Chapter One")
        self.assertEqual(with_proj.project_title, "My Novel")

        nested = parse_author_command(
            "Create a document called Notes in Progress in My Novel."
        )
        self.assertEqual(nested.title, "Notes in Progress")
        self.assertEqual(nested.project_title, "My Novel")

        missing = parse_author_command("Create a document called Chapter One.")
        self.assertEqual(missing.kind, "create_document")
        self.assertIsNone(missing.project_title)

    def test_ordinary_author_chat_is_not_a_command(self):
        self.assertIsNone(parse_author_command("What should happen in chapter two?"))
        self.assertIsNone(parse_author_command("Open Author."))


class AuthorActionsChatTests(unittest.TestCase):
    def setUp(self):
        use_memory_store(True)
        author_store.use_memory_store(True)
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._create = patch("shared.db.create_conversation", new_callable=AsyncMock)
        self._get = patch(
            "shared.db.get_conversation", new_callable=AsyncMock, return_value=None
        )
        self._load = patch(
            "shared.db.load_messages", new_callable=AsyncMock, return_value=[]
        )
        self._append = patch("shared.db.append_message", new_callable=AsyncMock)
        for p in (
            self._pool_init,
            self._pool_close,
            self._create,
            self._get,
            self._load,
        ):
            p.start()
            self.addCleanup(p.stop)
        self.append_mock = self._append.start()
        self.addCleanup(self._append.stop)
        self.addCleanup(lambda: use_memory_store(False))
        self.addCleanup(lambda: author_store.use_memory_store(False))

    def _register(self, client, username: str, password: str = "password123"):  # pragma: allowlist secret
        return client.post(
            "/auth/register",
            json={"username": username, "password": password},
        )

    def _chat(self, client, token: str, transcript: str, *, mode: str = "author"):
        return client.post(
            "/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "transcript": transcript,
                "mode": mode,
                "conversation_id": None,
            },
        )

    def test_create_project_via_chat_mutates_and_returns_result_action(self):
        with _env():
            with TestClient(app) as client:
                reg = self._register(client, "alice").json()
                token = reg["access_token"]
                user_id = reg["user"]["id"]
                with patch("main._run_model_turn", new_callable=AsyncMock) as turn:
                    resp = self._chat(
                        client,
                        token,
                        "Create an Author project called My Novel.",
                        mode="fitness",
                    )
                self.assertEqual(resp.status_code, 200, resp.text)
                body = resp.json()
                self.assertEqual(body["reply"], "I created My Novel.")
                self.assertEqual(len(body["client_actions"]), 1)
                action = body["client_actions"][0]
                self.assertEqual(action["type"], "author_project_created")
                self.assertEqual(action["title"], "My Novel")
                self.assertTrue(action["project_id"])
                turn.assert_not_called()

                # Exists via Author REST; owned by JWT user.
                got = client.get(
                    f"/author/projects/{action['project_id']}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(got.status_code, 200, got.text)
                self.assertEqual(got.json()["title"], "My Novel")
                stored = author_store._mem().projects[action["project_id"]]
                self.assertEqual(stored["user_id"], user_id)

                # Ack persisted into conversation history.
                self.append_mock.assert_any_call(
                    body["conversation_id"],
                    "user",
                    "Create an Author project called My Novel.",
                )
                self.append_mock.assert_any_call(
                    body["conversation_id"], "assistant", "I created My Novel."
                )

    def test_create_document_unique_project_works(self):
        with _env():
            with TestClient(app) as client:
                token = self._register(client, "alice").json()["access_token"]
                h = {"Authorization": f"Bearer {token}"}
                project = client.post(
                    "/author/projects", headers=h, json={"title": "My Novel"}
                ).json()
                resp = self._chat(
                    client,
                    token,
                    "Create a document called Chapter One in My Novel.",
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                body = resp.json()
                self.assertIn("Chapter One", body["reply"])
                action = body["client_actions"][0]
                self.assertEqual(action["type"], "author_document_created")
                self.assertEqual(action["project_id"], project["id"])
                self.assertEqual(action["title"], "Chapter One")
                doc = client.get(
                    f"/author/documents/{action['document_id']}", headers=h
                )
                self.assertEqual(doc.status_code, 200)
                self.assertEqual(doc.json()["project_id"], project["id"])

    def test_create_document_missing_project_asks_no_mutation(self):
        with _env():
            with TestClient(app) as client:
                token = self._register(client, "alice").json()["access_token"]
                before = len(author_store._mem().documents)
                resp = self._chat(
                    client, token, "Create a document called Chapter One."
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                body = resp.json()
                self.assertEqual(body["client_actions"], [])
                self.assertIn("Which Author project", body["reply"])
                self.assertEqual(len(author_store._mem().documents), before)

    def test_create_document_unknown_project_asks_no_mutation(self):
        with _env():
            with TestClient(app) as client:
                token = self._register(client, "alice").json()["access_token"]
                before = len(author_store._mem().documents)
                resp = self._chat(
                    client,
                    token,
                    "Create a document called Chapter One in Missing Book.",
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["client_actions"], [])
                self.assertIn("couldn't find", resp.json()["reply"].lower())
                self.assertEqual(len(author_store._mem().documents), before)

    def test_create_document_ambiguous_project_asks_no_mutation(self):
        with _env():
            with TestClient(app) as client:
                token = self._register(client, "alice").json()["access_token"]
                h = {"Authorization": f"Bearer {token}"}
                client.post("/author/projects", headers=h, json={"title": "My Novel"})
                client.post("/author/projects", headers=h, json={"title": "My Novel"})
                before = len(author_store._mem().documents)
                resp = self._chat(
                    client,
                    token,
                    "Create a document called Chapter One in My Novel.",
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                body = resp.json()
                self.assertEqual(body["client_actions"], [])
                self.assertIn("more than one project", body["reply"])
                self.assertEqual(len(author_store._mem().documents), before)

    def test_ownership_isolated_between_users(self):
        with _env():
            with TestClient(app) as client:
                a = self._register(client, "alice").json()
                b = self._register(client, "bob").json()
                a_chat = self._chat(
                    client,
                    a["access_token"],
                    "Create an Author project called Secret Draft.",
                ).json()
                project_id = a_chat["client_actions"][0]["project_id"]
                bob_get = client.get(
                    f"/author/projects/{project_id}",
                    headers={"Authorization": f"Bearer {b['access_token']}"},
                )
                self.assertEqual(bob_get.status_code, 404)
                # Bob creating a same-titled project does not steal Alice's.
                b_chat = self._chat(
                    client,
                    b["access_token"],
                    "Create an Author project called Secret Draft.",
                ).json()
                self.assertNotEqual(
                    b_chat["client_actions"][0]["project_id"], project_id
                )

    def test_ordinary_author_conversation_does_not_mutate(self):
        with _env():
            with TestClient(app) as client:
                token = self._register(client, "alice").json()["access_token"]
                before_p = len(author_store._mem().projects)
                before_d = len(author_store._mem().documents)
                with patch(
                    "main._run_model_turn",
                    new_callable=AsyncMock,
                    return_value=("Try a colder opening line.", None),
                ) as turn:
                    resp = self._chat(
                        client,
                        token,
                        "What should happen in chapter two?",
                    )
                self.assertEqual(resp.status_code, 200, resp.text)
                body = resp.json()
                self.assertEqual(body["client_actions"], [])
                self.assertEqual(body["reply"], "Try a colder opening line.")
                self.assertEqual(len(author_store._mem().projects), before_p)
                self.assertEqual(len(author_store._mem().documents), before_d)
                turn.assert_called_once()

    def test_navigation_still_works(self):
        with _env():
            with TestClient(app) as client:
                token = self._register(client, "alice").json()["access_token"]
                resp = self._chat(client, token, "Open Author.", mode="diet")
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(
                    resp.json()["client_actions"],
                    [{"type": "navigate", "target": "author"}],
                )

    def test_union_wire_shapes_and_navigate_compat(self):
        payload = ChatResponse(
            reply="I created My Novel.",
            mode="author",
            conversation_id="00000000-0000-4000-8000-000000000099",
            client_actions=[
                AuthorProjectCreatedAction(
                    type="author_project_created",
                    project_id="00000000-0000-4000-8000-000000000001",
                    title="My Novel",
                ),
                NavigateAction(type="navigate", target="author"),
            ],
        ).model_dump(mode="json")
        self.assertEqual(payload["client_actions"][0]["type"], "author_project_created")
        self.assertEqual(payload["client_actions"][1]["type"], "navigate")
        doc_payload = AuthorDocumentCreatedAction(
            type="author_document_created",
            project_id="p1",
            document_id="d1",
            title="Chapter One",
        ).model_dump(mode="json")
        self.assertEqual(
            doc_payload,
            {
                "type": "author_document_created",
                "project_id": "p1",
                "document_id": "d1",
                "title": "Chapter One",
            },
        )


if __name__ == "__main__":
    unittest.main()
