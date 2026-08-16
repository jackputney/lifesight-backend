"""Author capture pipeline — memory store + TestClient, no network.

The refinement model call is patched everywhere, so these tests never touch
Anthropic and never need Postgres.

Run:  python -m unittest tests.test_author_pipeline_v1 -v
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from shared.author_pipeline import store as pipeline_store
from shared.local_auth.store import use_memory_store

REPO = Path(__file__).resolve().parents[1]
MIGRATION_017 = REPO / "migrations" / "017_author_capture_pipeline.sql"

RAW_ONE = "um, so — the light was, uh, weird… you know?"
RAW_TWO = "and then she said nothing at all, which was the loudest part"

REFINED = "This is the first thing I said. And this is the second thing."


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "self",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


def _span(needle: str) -> tuple[int, int]:
    start = REFINED.index(needle)
    return start, start + len(needle)


def _flag(category: str, needle: str | None, explanation: str, suggested: str | None):
    if needle is None:
        return {
            "category": category,
            "span_start": None,
            "span_end": None,
            "explanation": explanation,
            "suggested_change": suggested,
        }
    start, end = _span(needle)
    return {
        "category": category,
        "span_start": start,
        "span_end": end,
        "explanation": explanation,
        "suggested_change": suggested,
    }


DEFAULT_FLAGS = [
    _flag("grammar", "This", "The opening word may point at the wrong thing.", "That"),
    _flag("typo", "first", "This may have been misheard.", "1st"),
    _flag("repetition", "second", "You used a similar word a moment ago.", "next"),
    _flag("tangent", None, "The middle drifts away from the scene you started.", None),
    _flag("unclear", None, "It is not clear who is speaking here.", None),
]


def _model_reply(content: str = REFINED, flags: list | None = None) -> str:
    return json.dumps(
        {"content": content, "flags": DEFAULT_FLAGS if flags is None else flags}
    )


class Migration017Tests(unittest.TestCase):
    def test_schema_tables_keys_and_immutability_trigger(self):
        text = MIGRATION_017.read_text(encoding="utf-8")
        for table in (
            "author_sessions",
            "author_captures",
            "author_draft_versions",
            "author_flags",
            "author_flag_decisions",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", text)

        self.assertEqual(text.count("REFERENCES public.users(id) ON DELETE CASCADE"), 5)
        self.assertNotIn("REFERENCES auth.", text)
        self.assertIn("REFERENCES author_sessions(id) ON DELETE CASCADE", text)
        self.assertIn("REFERENCES author_flags(id) ON DELETE CASCADE", text)
        self.assertIn("UNIQUE (session_id, sequence)", text)
        self.assertIn("UNIQUE (session_id, version)", text)

    def test_database_layer_blocks_capture_update_and_delete(self):
        text = MIGRATION_017.read_text(encoding="utf-8")
        self.assertIn("CREATE OR REPLACE FUNCTION author_captures_reject_mutation", text)
        self.assertIn("RAISE EXCEPTION", text)
        self.assertIn("BEFORE UPDATE OR DELETE ON author_captures", text)
        self.assertIn("EXECUTE FUNCTION author_captures_reject_mutation()", text)

    def test_edit_decision_requires_replacement_text_in_schema(self):
        text = MIGRATION_017.read_text(encoding="utf-8")
        self.assertIn("decision <> 'edit' OR replacement_text IS NOT NULL", text)


class NoCaptureMutationSourceTests(unittest.TestCase):
    """Application layer: no UPDATE/DELETE statement targets author_captures."""

    def test_store_never_updates_or_deletes_captures(self):
        text = (REPO / "shared" / "author_pipeline" / "store.py").read_text(encoding="utf-8")
        lowered = text.lower()
        self.assertNotIn("update author_captures", lowered)
        self.assertNotIn("delete from author_captures", lowered)

    def test_router_exposes_no_capture_mutation_route(self):
        text = (REPO / "routers" / "author_pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("@router.patch", text)
        self.assertNotIn("@router.put", text)
        self.assertNotIn("@router.delete", text)


class AuthorPipelineTests(unittest.TestCase):
    def setUp(self):
        use_memory_store(True)
        pipeline_store.use_memory_store(True)
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._pool_init.start()
        self._pool_close.start()
        self.model = Mock(return_value=_model_reply())
        self._model_patch = patch(
            "shared.author_pipeline.refine.call_model", self.model
        )
        self._model_patch.start()
        self.addCleanup(self._model_patch.stop)
        self.addCleanup(self._pool_init.stop)
        self.addCleanup(self._pool_close.stop)
        self.addCleanup(lambda: use_memory_store(False))
        self.addCleanup(lambda: pipeline_store.use_memory_store(False))

    def _client(self):
        from main import app

        # The coordinator wires this router into main.py; register it here when
        # that has not landed yet so the suite is order-independent either way.
        from routers.author_pipeline import router as pipeline_router

        if not any(getattr(r, "path", "") == "/author/sessions" for r in app.routes):
            app.include_router(pipeline_router)
        return TestClient(app)

    def _register(self, client, username: str, password: str = "password123"):  # pragma: allowlist secret
        return client.post(
            "/auth/register",
            json={"username": username, "password": password},
        )

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _two_users(self, client):
        a = self._register(client, "pipeline_a").json()
        b = self._register(client, "pipeline_b").json()
        return a["access_token"], b["access_token"]

    def _session_with_captures(self, client, headers) -> str:
        session = client.post(
            "/author/sessions", headers=headers, json={"title": "Chapter three"}
        ).json()
        sid = session["id"]
        client.post(
            f"/author/sessions/{sid}/captures",
            headers=headers,
            json={"source": "voice", "raw_text": RAW_ONE},
        )
        client.post(
            f"/author/sessions/{sid}/captures",
            headers=headers,
            json={"source": "typed", "raw_text": RAW_TWO},
        )
        return sid

    def _refine(self, client, headers, sid, body=None):
        return client.post(
            f"/author/sessions/{sid}/refine", headers=headers, json=body or {}
        )

    def _flags_by_category(self, flags: list[dict]) -> dict[str, dict]:
        return {f["category"]: f for f in flags}

    # -- capture -------------------------------------------------------------

    def test_captures_append_with_sequential_sequence(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                session = client.post(
                    "/author/sessions", headers=h, json={"title": "Dictation"}
                )
                self.assertEqual(session.status_code, 200, session.text)
                sid = session.json()["id"]
                self.assertEqual(session.json()["status"], "active")
                self.assertNotIn("user_id", session.json())

                bodyless = client.post("/author/sessions", headers=h)
                self.assertEqual(bodyless.status_code, 200, bodyless.text)
                self.assertIsNone(bodyless.json()["title"])

                sequences = []
                for index, text in enumerate([RAW_ONE, RAW_TWO, "a third thought"]):
                    created = client.post(
                        f"/author/sessions/{sid}/captures",
                        headers=h,
                        json={"source": "voice" if index % 2 == 0 else "typed",
                              "raw_text": text},
                    )
                    self.assertEqual(created.status_code, 200, created.text)
                    sequences.append(created.json()["sequence"])
                self.assertEqual(sequences, [0, 1, 2])

                page = client.get(f"/author/sessions/{sid}/captures", headers=h)
                self.assertEqual(page.status_code, 200)
                body = page.json()
                self.assertEqual(body["total"], 3)
                self.assertEqual(body["limit"], pipeline_store.DEFAULT_PAGE_LIMIT)
                self.assertEqual([c["raw_text"] for c in body["items"]][0], RAW_ONE)

    def test_capture_path_has_no_mutating_methods(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                path = f"/author/sessions/{sid}/captures"
                for call in (client.patch, client.put, client.delete):
                    response = call(path, headers=h)
                    self.assertEqual(response.status_code, 405, response.text)

    def test_raw_capture_survives_refinement_and_decisions_byte_identical(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                before = client.get(f"/author/sessions/{sid}/captures", headers=h).json()

                refined = self._refine(client, h, sid)
                self.assertEqual(refined.status_code, 200, refined.text)
                flags = self._flags_by_category(refined.json()["flags"])
                for category, payload in (
                    ("grammar", {"decision": "accept"}),
                    ("typo", {"decision": "edit", "replacement_text": "very first"}),
                    ("repetition", {"decision": "reject"}),
                    ("tangent", {"decision": "defer"}),
                ):
                    decided = client.post(
                        f"/author/flags/{flags[category]['id']}/decision",
                        headers=h,
                        json=payload,
                    )
                    self.assertEqual(decided.status_code, 200, decided.text)

                after = client.get(f"/author/sessions/{sid}/captures", headers=h).json()
                self.assertEqual(before["items"], after["items"])
                self.assertEqual(after["items"][0]["raw_text"], RAW_ONE)
                self.assertEqual(after["items"][1]["raw_text"], RAW_TWO)

    def test_session_detail_separates_raw_captures_from_refined_versions(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                self._refine(client, h, sid)

                detail = client.get(f"/author/sessions/{sid}", headers=h)
                self.assertEqual(detail.status_code, 200, detail.text)
                body = detail.json()
                self.assertEqual(body["session"]["id"], sid)
                self.assertEqual(
                    [c["raw_text"] for c in body["captures"]], [RAW_ONE, RAW_TWO]
                )
                self.assertEqual(len(body["draft_versions"]), 1)
                self.assertEqual(body["draft_versions"][0]["content"], REFINED)
                self.assertEqual(len(body["open_flags"]), len(DEFAULT_FLAGS))

    # -- refinement ----------------------------------------------------------

    def test_refine_creates_new_version_and_leaves_prior_versions_intact(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)

                first = self._refine(client, h, sid).json()["draft_version"]
                self.assertEqual(first["version"], 1)
                self.assertEqual(first["source_capture_from"], 0)
                self.assertEqual(first["source_capture_to"], 1)
                self.assertIsNone(first["derived_from_version_id"])

                self.model.return_value = _model_reply(content="A tighter second pass.")
                second = self._refine(
                    client, h, sid, {"refinement_level": "polish"}
                ).json()["draft_version"]
                self.assertEqual(second["version"], 2)
                self.assertEqual(second["refinement_level"], "polish")
                self.assertEqual(second["content"], "A tighter second pass.")

                versions = client.get(f"/author/sessions/{sid}", headers=h).json()[
                    "draft_versions"
                ]
                self.assertEqual([v["version"] for v in versions], [2, 1])
                original = next(v for v in versions if v["version"] == 1)
                self.assertEqual(original["content"], REFINED)

    def test_preserve_voice_is_the_default_level(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)

                omitted = self._refine(client, h, sid, {}).json()["draft_version"]
                self.assertEqual(omitted["refinement_level"], "preserve_voice")

                explicit_null = self._refine(
                    client, h, sid, {"refinement_level": None}
                ).json()["draft_version"]
                self.assertEqual(explicit_null["refinement_level"], "preserve_voice")

                bodyless = client.post(f"/author/sessions/{sid}/refine", headers=h)
                self.assertEqual(bodyless.status_code, 200, bodyless.text)
                self.assertEqual(
                    bodyless.json()["draft_version"]["refinement_level"], "preserve_voice"
                )

                system_prompt = self.model.call_args.args[0]
                self.assertIn("preserve_voice", system_prompt)
                self.assertIn("distinctive voice", system_prompt)
                self.assertIn("generic polished AI prose", system_prompt)

    def test_refine_honours_capture_range(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)

                ranged = self._refine(
                    client, h, sid, {"capture_from": 1, "capture_to": 1}
                ).json()["draft_version"]
                self.assertEqual(ranged["source_capture_from"], 1)
                self.assertEqual(ranged["source_capture_to"], 1)
                user_prompt = self.model.call_args.args[1]
                self.assertIn(RAW_TWO, user_prompt)
                self.assertNotIn(RAW_ONE, user_prompt)

                empty = self._refine(
                    client, h, sid, {"capture_from": 9, "capture_to": 12}
                )
                self.assertEqual(empty.status_code, 400)

                inverted = self._refine(
                    client, h, sid, {"capture_from": 3, "capture_to": 1}
                )
                self.assertEqual(inverted.status_code, 400)

    def test_refine_without_captures_is_rejected(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = client.post("/author/sessions", headers=h, json={}).json()["id"]
                response = self._refine(client, h, sid)
                self.assertEqual(response.status_code, 400)
                self.model.assert_not_called()

    def test_unparseable_model_reply_is_502_and_stores_nothing(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)

                self.model.return_value = "I refined it for you! (no JSON here)"
                response = self._refine(client, h, sid)
                self.assertEqual(response.status_code, 502, response.text)

                self.model.return_value = json.dumps({"content": "  "})
                self.assertEqual(self._refine(client, h, sid).status_code, 502)

                self.model.return_value = json.dumps(
                    {"content": REFINED, "flags": "not-a-list"}
                )
                self.assertEqual(self._refine(client, h, sid).status_code, 502)

                detail = client.get(f"/author/sessions/{sid}", headers=h).json()
                self.assertEqual(detail["draft_versions"], [])
                self.assertEqual(detail["open_flags"], [])

    def test_flags_are_separate_rows_and_never_applied_to_the_text(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)

                body = self._refine(client, h, sid).json()
                self.assertEqual(body["draft_version"]["content"], REFINED)
                flags = body["flags"]
                self.assertEqual(len(flags), len(DEFAULT_FLAGS))
                for flag in flags:
                    self.assertEqual(flag["status"], "open")
                    self.assertEqual(flag["draft_version_id"], body["draft_version"]["id"])
                    self.assertTrue(flag["explanation"])

                grammar = self._flags_by_category(flags)["grammar"]
                self.assertEqual(grammar["suggested_change"], "That")
                self.assertEqual(REFINED[grammar["span_start"]: grammar["span_end"]], "This")

                # An advisory flag keeps no un-appliable suggestion.
                advisory = self._flags_by_category(flags)["tangent"]
                self.assertIsNone(advisory["span_start"])
                self.assertIsNone(advisory["suggested_change"])

    # -- decisions -----------------------------------------------------------

    def test_accept_creates_new_version_with_suggested_change_applied(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                refined = self._refine(client, h, sid).json()
                source = refined["draft_version"]
                flag = self._flags_by_category(refined["flags"])["grammar"]

                response = client.post(
                    f"/author/flags/{flag['id']}/decision",
                    headers=h,
                    json={"decision": "accept"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["flag"]["status"], "accepted")
                self.assertEqual(body["decision"]["decision"], "accept")
                new_version = body["draft_version"]
                self.assertIsNotNone(new_version)
                self.assertEqual(new_version["version"], 2)
                self.assertEqual(new_version["derived_from_version_id"], source["id"])
                self.assertIsNone(new_version["model_identifier"])
                self.assertEqual(new_version["content"], REFINED.replace("This", "That", 1))
                self.assertEqual(
                    body["decision"]["resulting_draft_version_id"], new_version["id"]
                )

                versions = client.get(f"/author/sessions/{sid}", headers=h).json()[
                    "draft_versions"
                ]
                original = next(v for v in versions if v["version"] == 1)
                self.assertEqual(original["content"], REFINED)

    def test_edit_applies_replacement_text_and_creates_new_version(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                refined = self._refine(client, h, sid).json()
                flag = self._flags_by_category(refined["flags"])["typo"]

                response = client.post(
                    f"/author/flags/{flag['id']}/decision",
                    headers=h,
                    json={"decision": "edit", "replacement_text": "very first"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["flag"]["status"], "edited")
                self.assertEqual(body["decision"]["replacement_text"], "very first")
                self.assertEqual(
                    body["draft_version"]["content"],
                    REFINED.replace("first", "very first", 1),
                )

    def test_edit_without_replacement_text_is_400(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                refined = self._refine(client, h, sid).json()
                flag = self._flags_by_category(refined["flags"])["typo"]

                response = client.post(
                    f"/author/flags/{flag['id']}/decision",
                    headers=h,
                    json={"decision": "edit"},
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(
                    client.get(f"/author/sessions/{sid}", headers=h).json()[
                        "draft_versions"
                    ][0]["content"],
                    REFINED,
                )

    def test_reject_and_defer_change_no_text_and_create_no_version(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                refined = self._refine(client, h, sid).json()
                flags = self._flags_by_category(refined["flags"])

                rejected = client.post(
                    f"/author/flags/{flags['repetition']['id']}/decision",
                    headers=h,
                    json={"decision": "reject"},
                ).json()
                self.assertEqual(rejected["flag"]["status"], "rejected")
                self.assertIsNone(rejected["draft_version"])
                self.assertIsNone(rejected["decision"]["resulting_draft_version_id"])

                deferred = client.post(
                    f"/author/flags/{flags['tangent']['id']}/decision",
                    headers=h,
                    json={"decision": "defer"},
                ).json()
                self.assertEqual(deferred["flag"]["status"], "deferred")
                self.assertIsNone(deferred["draft_version"])

                detail = client.get(f"/author/sessions/{sid}", headers=h).json()
                self.assertEqual(len(detail["draft_versions"]), 1)
                self.assertEqual(detail["draft_versions"][0]["content"], REFINED)
                self.assertEqual(
                    [f["category"] for f in detail["open_flags"]],
                    ["grammar", "typo", "unclear"],
                )

    def test_accepting_an_advisory_flag_is_400(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                refined = self._refine(client, h, sid).json()
                flag = self._flags_by_category(refined["flags"])["unclear"]

                response = client.post(
                    f"/author/flags/{flag['id']}/decision",
                    headers=h,
                    json={"decision": "accept"},
                )
                self.assertEqual(response.status_code, 400, response.text)

    def test_second_decision_on_the_same_flag_is_409(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                refined = self._refine(client, h, sid).json()
                flag = self._flags_by_category(refined["flags"])["repetition"]
                path = f"/author/flags/{flag['id']}/decision"

                self.assertEqual(
                    client.post(path, headers=h, json={"decision": "reject"}).status_code,
                    200,
                )
                self.assertEqual(
                    client.post(path, headers=h, json={"decision": "accept"}).status_code,
                    409,
                )

    # -- session lifecycle ---------------------------------------------------

    def test_end_session_blocks_further_captures(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)

                ended = client.post(f"/author/sessions/{sid}/end", headers=h)
                self.assertEqual(ended.status_code, 200, ended.text)
                self.assertEqual(ended.json()["status"], "ended")
                self.assertIsNotNone(ended.json()["ended_at"])

                blocked = client.post(
                    f"/author/sessions/{sid}/captures",
                    headers=h,
                    json={"source": "voice", "raw_text": "one more thing"},
                )
                self.assertEqual(blocked.status_code, 409)

                # Review of an ended session still works.
                self.assertEqual(self._refine(client, h, sid).status_code, 200)

    def test_session_listing_is_paginated_and_owner_scoped(self):
        with _env():
            with self._client() as client:
                token_a, token_b = self._two_users(client)
                ha, hb = self._auth(token_a), self._auth(token_b)
                for index in range(3):
                    client.post(
                        "/author/sessions", headers=ha, json={"title": f"Session {index}"}
                    )
                page = client.get("/author/sessions?limit=2&offset=0", headers=ha).json()
                self.assertEqual(page["total"], 3)
                self.assertEqual(len(page["items"]), 2)
                self.assertEqual(page["limit"], 2)
                self.assertEqual(page["offset"], 0)

                self.assertEqual(
                    client.get("/author/sessions", headers=hb).json()["total"], 0
                )

    # -- isolation -----------------------------------------------------------

    def test_cross_user_access_is_404_everywhere(self):
        with _env():
            with self._client() as client:
                token_a, token_b = self._two_users(client)
                ha, hb = self._auth(token_a), self._auth(token_b)
                sid = self._session_with_captures(client, ha)
                refined = self._refine(client, ha, sid).json()
                flag_id = refined["flags"][0]["id"]
                self.model.reset_mock()

                self.assertEqual(
                    client.get(f"/author/sessions/{sid}", headers=hb).status_code, 404
                )
                self.assertEqual(
                    client.get(f"/author/sessions/{sid}/captures", headers=hb).status_code,
                    404,
                )
                self.assertEqual(
                    client.post(
                        f"/author/sessions/{sid}/captures",
                        headers=hb,
                        json={"source": "voice", "raw_text": "not mine"},
                    ).status_code,
                    404,
                )
                self.assertEqual(self._refine(client, hb, sid).status_code, 404)
                self.assertEqual(
                    client.post(f"/author/sessions/{sid}/end", headers=hb).status_code, 404
                )
                self.assertEqual(
                    client.post(
                        f"/author/flags/{flag_id}/decision",
                        headers=hb,
                        json={"decision": "accept"},
                    ).status_code,
                    404,
                )
                # A blocked refine must never reach the model.
                self.model.assert_not_called()

                # Owner is untouched.
                detail = client.get(f"/author/sessions/{sid}", headers=ha).json()
                self.assertEqual(
                    [c["raw_text"] for c in detail["captures"]], [RAW_ONE, RAW_TWO]
                )
                self.assertEqual(detail["open_flags"][0]["status"], "open")

    def test_unauthenticated_requests_are_rejected(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                refined = self._refine(client, h, sid).json()
                flag_id = refined["flags"][0]["id"]

                self.assertEqual(client.get("/author/sessions").status_code, 401)
                self.assertEqual(
                    client.post("/author/sessions", json={"title": "x"}).status_code, 401
                )
                self.assertEqual(client.get(f"/author/sessions/{sid}").status_code, 401)
                self.assertEqual(
                    client.get(f"/author/sessions/{sid}/captures").status_code, 401
                )
                self.assertEqual(
                    client.post(
                        f"/author/sessions/{sid}/captures",
                        json={"source": "voice", "raw_text": "anonymous"},
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    client.post(f"/author/sessions/{sid}/refine", json={}).status_code, 401
                )
                self.assertEqual(
                    client.post(
                        f"/author/flags/{flag_id}/decision", json={"decision": "reject"}
                    ).status_code,
                    401,
                )

    def test_ownership_cannot_be_asserted_in_the_request_body(self):
        with _env():
            with self._client() as client:
                token_a, token_b = self._two_users(client)
                ha = self._auth(token_a)
                other_user = "00000000-0000-4000-8000-000000000099"
                created = client.post(
                    "/author/sessions",
                    headers=ha,
                    json={"title": "Inject", "user_id": other_user},
                )
                self.assertEqual(created.status_code, 200, created.text)
                stored = pipeline_store._memory.sessions[created.json()["id"]]
                self.assertNotEqual(stored["user_id"], other_user)


if __name__ == "__main__":
    unittest.main()
