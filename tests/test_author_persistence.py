"""Author projects / documents / versions — memory store + TestClient.

Run:  python -m unittest tests.test_author_persistence -v
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from shared.author_persistence import store as author_store
from shared.local_auth.store import use_memory_store

REPO = Path(__file__).resolve().parents[1]
MIGRATION_008 = REPO / "migrations" / "008_author_persistence.sql"


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "self",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class Migration008Tests(unittest.TestCase):
    def test_schema_references_public_users_and_cascades(self):
        text = MIGRATION_008.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS author_projects", text)
        self.assertIn("CREATE TABLE IF NOT EXISTS author_documents", text)
        self.assertIn("CREATE TABLE IF NOT EXISTS author_document_versions", text)
        self.assertGreaterEqual(text.count("REFERENCES users(id) ON DELETE CASCADE"), 3)
        self.assertNotIn("REFERENCES auth.", text)
        self.assertIn("REFERENCES author_projects(id) ON DELETE CASCADE", text)
        self.assertIn("REFERENCES author_documents(id) ON DELETE CASCADE", text)
        self.assertIn("UNIQUE (document_id, revision)", text)


class AuthorPersistenceTests(unittest.TestCase):
    def setUp(self):
        use_memory_store(True)
        author_store.use_memory_store(True)
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._pool_init.start()
        self._pool_close.start()
        self.addCleanup(self._pool_init.stop)
        self.addCleanup(self._pool_close.stop)
        self.addCleanup(lambda: use_memory_store(False))
        self.addCleanup(lambda: author_store.use_memory_store(False))

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
        a = self._register(client, "author_a").json()
        b = self._register(client, "author_b").json()
        return a["access_token"], b["access_token"]

    def test_project_crud_and_pagination(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                created = client.post(
                    "/author/projects",
                    headers=h,
                    json={"title": "Novel", "description": "Draft one"},
                )
                self.assertEqual(created.status_code, 200, created.text)
                project = created.json()
                self.assertEqual(project["title"], "Novel")
                self.assertNotIn("user_id", project)

                got = client.get(f"/author/projects/{project['id']}", headers=h)
                self.assertEqual(got.status_code, 200)
                self.assertEqual(got.json()["id"], project["id"])

                patched = client.patch(
                    f"/author/projects/{project['id']}",
                    headers=h,
                    json={"title": "Novel Revised"},
                )
                self.assertEqual(patched.status_code, 200)
                self.assertEqual(patched.json()["title"], "Novel Revised")

                for i in range(3):
                    client.post(
                        "/author/projects",
                        headers=h,
                        json={"title": f"Extra {i}"},
                    )
                page = client.get("/author/projects?limit=2&offset=0", headers=h)
                self.assertEqual(page.status_code, 200)
                body = page.json()
                self.assertEqual(body["limit"], 2)
                self.assertEqual(body["offset"], 0)
                self.assertEqual(len(body["items"]), 2)
                self.assertEqual(body["total"], 4)

                deleted = client.delete(f"/author/projects/{project['id']}", headers=h)
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(
                    client.get(f"/author/projects/{project['id']}", headers=h).status_code,
                    404,
                )

    def test_document_crud_versions_and_autosave_conflict(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                project = client.post(
                    "/author/projects", headers=h, json={"title": "Book"}
                ).json()
                pid = project["id"]

                created = client.post(
                    f"/author/projects/{pid}/documents",
                    headers=h,
                    json={"title": "Chapter 1", "content": "Once upon a time"},
                )
                self.assertEqual(created.status_code, 200, created.text)
                doc = created.json()
                self.assertEqual(doc["revision"], 1)
                self.assertEqual(doc["content"], "Once upon a time")

                versions = client.get(
                    f"/author/documents/{doc['id']}/versions", headers=h
                )
                self.assertEqual(versions.status_code, 200)
                vbody = versions.json()
                self.assertEqual(vbody["total"], 1)
                self.assertEqual(vbody["items"][0]["revision"], 1)

                updated = client.patch(
                    f"/author/documents/{doc['id']}",
                    headers=h,
                    json={
                        "expected_revision": 1,
                        "content": "Once upon a time, revised",
                    },
                )
                self.assertEqual(updated.status_code, 200)
                self.assertEqual(updated.json()["revision"], 2)
                self.assertEqual(updated.json()["content"], "Once upon a time, revised")

                conflict = client.patch(
                    f"/author/documents/{doc['id']}",
                    headers=h,
                    json={"expected_revision": 1, "content": "stale write"},
                )
                self.assertEqual(conflict.status_code, 409)
                detail = conflict.json()["detail"]
                self.assertEqual(detail["current"]["revision"], 2)

                checkpoint = client.post(
                    f"/author/documents/{doc['id']}/versions", headers=h
                )
                self.assertEqual(checkpoint.status_code, 200)
                self.assertEqual(checkpoint.json()["revision"], 3)

                listed = client.get(
                    f"/author/documents/{doc['id']}/versions", headers=h
                ).json()
                self.assertEqual(listed["total"], 3)
                revs = [item["revision"] for item in listed["items"]]
                self.assertEqual(revs, [3, 2, 1])

                # History is preserved: revision 1 content unchanged
                v1 = next(i for i in listed["items"] if i["revision"] == 1)
                one = client.get(
                    f"/author/documents/{doc['id']}/versions/{v1['id']}",
                    headers=h,
                )
                self.assertEqual(one.status_code, 200)
                self.assertEqual(one.json()["content"], "Once upon a time")

                # Reject ownership in body (ignored / not present on model)
                bad = client.post(
                    f"/author/projects/{pid}/documents",
                    headers=h,
                    json={
                        "title": "Inject",
                        "content": "x",
                        "user_id": "00000000-0000-4000-8000-000000000099",
                    },
                )
                self.assertEqual(bad.status_code, 200)
                injected_id = bad.json()["id"]
                mem_doc = author_store._memory.documents[injected_id]
                self.assertNotEqual(
                    mem_doc["user_id"],
                    "00000000-0000-4000-8000-000000000099",
                )

    def test_cross_user_isolation_returns_404(self):
        with _env():
            with self._client() as client:
                token_a, token_b = self._two_users(client)
                ha, hb = self._auth(token_a), self._auth(token_b)
                project = client.post(
                    "/author/projects", headers=ha, json={"title": "A only"}
                ).json()
                doc = client.post(
                    f"/author/projects/{project['id']}/documents",
                    headers=ha,
                    json={"title": "Secret", "content": "private"},
                ).json()
                version = client.get(
                    f"/author/documents/{doc['id']}/versions", headers=ha
                ).json()["items"][0]

                self.assertEqual(
                    client.get(f"/author/projects/{project['id']}", headers=hb).status_code,
                    404,
                )
                self.assertEqual(
                    client.patch(
                        f"/author/projects/{project['id']}",
                        headers=hb,
                        json={"title": "hijack"},
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.delete(
                        f"/author/projects/{project['id']}", headers=hb
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        f"/author/projects/{project['id']}/documents", headers=hb
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(f"/author/documents/{doc['id']}", headers=hb).status_code,
                    404,
                )
                self.assertEqual(
                    client.patch(
                        f"/author/documents/{doc['id']}",
                        headers=hb,
                        json={"expected_revision": 1, "content": "nope"},
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.delete(
                        f"/author/documents/{doc['id']}", headers=hb
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        f"/author/documents/{doc['id']}/versions", headers=hb
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        f"/author/documents/{doc['id']}/versions/{version['id']}",
                        headers=hb,
                    ).status_code,
                    404,
                )

                # A still intact
                self.assertEqual(
                    client.get(f"/author/documents/{doc['id']}", headers=ha).status_code,
                    200,
                )

    def test_deleted_parent_cascades_documents_and_versions(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                project = client.post(
                    "/author/projects", headers=h, json={"title": "Temp"}
                ).json()
                doc = client.post(
                    f"/author/projects/{project['id']}/documents",
                    headers=h,
                    json={"title": "Doc", "content": "body"},
                ).json()
                client.patch(
                    f"/author/documents/{doc['id']}",
                    headers=h,
                    json={"expected_revision": 1, "content": "body2"},
                )
                versions_before = client.get(
                    f"/author/documents/{doc['id']}/versions", headers=h
                ).json()
                self.assertGreaterEqual(versions_before["total"], 2)

                deleted = client.delete(f"/author/projects/{project['id']}", headers=h)
                self.assertEqual(deleted.status_code, 200)

                self.assertEqual(
                    client.get(f"/author/documents/{doc['id']}", headers=h).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        f"/author/documents/{doc['id']}/versions", headers=h
                    ).status_code,
                    404,
                )
                # Memory store: no orphan rows for this user under deleted parent
                self.assertFalse(
                    any(
                        d["project_id"] == project["id"]
                        for d in author_store._memory.documents.values()
                    )
                )
                self.assertFalse(
                    any(
                        v["document_id"] == doc["id"]
                        for v in author_store._memory.versions.values()
                    )
                )


if __name__ == "__main__":
    unittest.main()
