"""Shared artifacts / versions — memory store + TestClient.

Run:  python -m unittest tests.test_artifacts -v
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from shared.artifacts import store as artifact_store
from shared.local_auth.store import use_memory_store

REPO = Path(__file__).resolve().parents[1]
MIGRATION_010 = REPO / "migrations" / "010_artifacts.sql"


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "self",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class Migration010Tests(unittest.TestCase):
    def test_schema_references_public_users_and_cascades(self):
        text = MIGRATION_010.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS artifacts", text)
        self.assertIn("CREATE TABLE IF NOT EXISTS artifact_versions", text)
        self.assertGreaterEqual(text.count("REFERENCES users(id) ON DELETE CASCADE"), 2)
        self.assertIn("REFERENCES artifacts(id) ON DELETE CASCADE", text)
        self.assertNotIn("REFERENCES auth.", text)
        self.assertIn("UNIQUE (artifact_id, revision)", text)
        self.assertIn("artifacts_user_updated_idx", text)
        self.assertIn("artifacts_user_type_updated_idx", text)
        self.assertIn("artifact_versions_artifact_rev_idx", text)
        self.assertIn("content      JSONB", text)
        self.assertNotIn("author_projects", text)


class ArtifactPersistenceTests(unittest.TestCase):
    def setUp(self):
        use_memory_store(True)
        artifact_store.use_memory_store(True)
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._pool_init.start()
        self._pool_close.start()
        self.addCleanup(self._pool_init.stop)
        self.addCleanup(self._pool_close.stop)
        self.addCleanup(lambda: use_memory_store(False))
        self.addCleanup(lambda: artifact_store.use_memory_store(False))

    def _client(self):
        from main import app

        return TestClient(app)

    def _register(self, client, username: str, password: str = "password123"):  # pragma: allowlist secret
        return client.post(
            "/auth/register",
            json={"username": username, "password": password},
        )

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _two_users(self, client):
        a = self._register(client, "artifact_a").json()
        b = self._register(client, "artifact_b").json()
        return a["access_token"], b["access_token"]

    def _create(self, client, headers, **overrides):
        body = {
            "type": "note",
            "title": "Scratch",
            "content": {"body": "hello"},
            "metadata": {"source": "test"},
        }
        body.update(overrides)
        return client.post("/artifacts", headers=headers, json=body)

    def test_create_get_list(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                created = self._create(client, h)
                self.assertEqual(created.status_code, 200, created.text)
                art = created.json()
                self.assertEqual(art["type"], "note")
                self.assertEqual(art["title"], "Scratch")
                self.assertEqual(art["content"], {"body": "hello"})
                self.assertEqual(art["revision"], 1)
                self.assertNotIn("user_id", art)

                got = client.get(f"/artifacts/{art['id']}", headers=h)
                self.assertEqual(got.status_code, 200)
                self.assertEqual(got.json()["id"], art["id"])

                listed = client.get("/artifacts", headers=h)
                self.assertEqual(listed.status_code, 200)
                body = listed.json()
                self.assertEqual(body["total"], 1)
                self.assertEqual(len(body["items"]), 1)

    def test_type_filter_and_pagination(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                for i in range(3):
                    self._create(client, h, type="note", title=f"Note {i}")
                for i in range(2):
                    self._create(client, h, type="plan", title=f"Plan {i}")

                notes = client.get("/artifacts?type=note", headers=h)
                self.assertEqual(notes.status_code, 200)
                self.assertEqual(notes.json()["total"], 3)
                self.assertTrue(all(i["type"] == "note" for i in notes.json()["items"]))

                page = client.get("/artifacts?limit=2&offset=0", headers=h)
                self.assertEqual(page.status_code, 200)
                body = page.json()
                self.assertEqual(body["limit"], 2)
                self.assertEqual(body["offset"], 0)
                self.assertEqual(len(body["items"]), 2)
                self.assertEqual(body["total"], 5)

    def test_update_increments_revision_and_appends_immutable_version(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                art = self._create(client, h).json()

                patched = client.patch(
                    f"/artifacts/{art['id']}",
                    headers=h,
                    json={
                        "expected_revision": 1,
                        "title": "Scratch v2",
                        "content": {"body": "updated"},
                    },
                )
                self.assertEqual(patched.status_code, 200, patched.text)
                updated = patched.json()
                self.assertEqual(updated["revision"], 2)
                self.assertEqual(updated["title"], "Scratch v2")
                self.assertEqual(updated["content"], {"body": "updated"})

                versions = client.get(f"/artifacts/{art['id']}/versions", headers=h)
                self.assertEqual(versions.status_code, 200)
                items = versions.json()["items"]
                self.assertEqual(versions.json()["total"], 2)
                self.assertEqual(items[0]["revision"], 2)
                self.assertEqual(items[0]["title"], "Scratch v2")
                self.assertEqual(items[1]["revision"], 1)

                # Versions are immutable: re-fetch revision 1 still has original content.
                v1 = client.get(
                    f"/artifacts/{art['id']}/versions/{items[1]['id']}",
                    headers=h,
                )
                self.assertEqual(v1.status_code, 200)
                self.assertEqual(v1.json()["content"], {"body": "hello"})

    def test_stale_revision_conflict_returns_current(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                art = self._create(client, h).json()
                client.patch(
                    f"/artifacts/{art['id']}",
                    headers=h,
                    json={"expected_revision": 1, "title": "Moved ahead"},
                )
                conflict = client.patch(
                    f"/artifacts/{art['id']}",
                    headers=h,
                    json={"expected_revision": 1, "title": "Stale write"},
                )
                self.assertEqual(conflict.status_code, 409)
                detail = conflict.json()["detail"]
                self.assertEqual(detail["message"], "Artifact revision conflict")
                self.assertEqual(detail["current"]["revision"], 2)
                self.assertEqual(detail["current"]["title"], "Moved ahead")
                self.assertEqual(detail["current"]["id"], art["id"])

    def test_explicit_version_checkpoint_does_not_mutate_revision(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                art = self._create(client, h).json()
                self.assertEqual(art["revision"], 1)

                checkpoint = client.post(
                    f"/artifacts/{art['id']}/versions",
                    headers=h,
                )
                self.assertEqual(checkpoint.status_code, 200, checkpoint.text)
                ver = checkpoint.json()
                self.assertEqual(ver["revision"], 1)
                self.assertEqual(ver["artifact_id"], art["id"])

                head = client.get(f"/artifacts/{art['id']}", headers=h).json()
                self.assertEqual(head["revision"], 1)

                # Idempotent: second checkpoint returns same revision snapshot.
                again = client.post(f"/artifacts/{art['id']}/versions", headers=h)
                self.assertEqual(again.status_code, 200)
                self.assertEqual(again.json()["id"], ver["id"])

    def test_ownership_isolation_returns_404(self):
        with _env():
            with self._client() as client:
                token_a, token_b = self._two_users(client)
                ha, hb = self._auth(token_a), self._auth(token_b)
                art = self._create(client, ha).json()

                self.assertEqual(
                    client.get(f"/artifacts/{art['id']}", headers=hb).status_code,
                    404,
                )
                self.assertEqual(
                    client.patch(
                        f"/artifacts/{art['id']}",
                        headers=hb,
                        json={"expected_revision": 1, "title": "Nope"},
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.delete(f"/artifacts/{art['id']}", headers=hb).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(f"/artifacts/{art['id']}/versions", headers=hb).status_code,
                    404,
                )

    def test_delete_cascades_versions(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                art = self._create(client, h).json()
                client.patch(
                    f"/artifacts/{art['id']}",
                    headers=h,
                    json={"expected_revision": 1, "title": "Before delete"},
                )
                versions = client.get(f"/artifacts/{art['id']}/versions", headers=h)
                self.assertEqual(versions.json()["total"], 2)
                version_id = versions.json()["items"][0]["id"]

                deleted = client.delete(f"/artifacts/{art['id']}", headers=h)
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(
                    client.get(f"/artifacts/{art['id']}", headers=h).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        f"/artifacts/{art['id']}/versions/{version_id}",
                        headers=h,
                    ).status_code,
                    404,
                )


if __name__ == "__main__":
    unittest.main()
