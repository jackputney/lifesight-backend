"""Author capture pipeline — memory store + TestClient, no network.

The refinement model call is patched everywhere, so these tests never touch
Anthropic and never need Postgres.

Run:  python -m unittest tests.test_author_pipeline_v1 -v
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import asyncpg
from fastapi import HTTPException
from fastapi.testclient import TestClient

from shared.author_persistence import store as persistence_store
from shared.author_pipeline import refine as pipeline_refine
from shared.author_pipeline import service as pipeline_service
from shared.author_pipeline import store as pipeline_store
from shared.local_auth.store import use_memory_store

REPO = Path(__file__).resolve().parents[1]
MIGRATION_017 = REPO / "migrations" / "017_author_capture_pipeline.sql"

# Directories that are not part of the shipped application source.
NON_SOURCE_DIRS = {"tests", ".venv", "venv", ".git", "__pycache__", "node_modules"}


def repo_python_sources() -> list[Path]:
    """Every shipped .py file in the repo — the scope the invariant actually has."""
    return [
        path
        for path in sorted(REPO.rglob("*.py"))
        if not NON_SOURCE_DIRS.intersection(path.relative_to(REPO).parts)
    ]


class _NoSqlPool:
    """Stand-in pool where reaching SQL at all is the failure being tested.

    A malformed path id must be rejected before it is ever bound to `$1::uuid`;
    real asyncpg answers such a bind with DataError, which surfaces as a 500.
    """

    def __init__(self):
        self.queries: list[str] = []

    async def _reject(self, query, *args):
        self.queries.append(query)
        raise asyncpg.DataError('invalid input syntax for type uuid: "not-a-uuid"')

    fetch = fetchrow = fetchval = execute = _reject


class _SequenceRacePool:
    """Loses the UNIQUE (session_id, sequence) race `failures` times, then wins."""

    def __init__(self, failures: int, row: dict):
        self.failures = failures
        self.row = row
        self.attempts = 0

    async def fetchrow(self, query, *args):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise asyncpg.UniqueViolationError(
                'duplicate key value violates unique constraint '
                '"author_captures_session_sequence_uidx"'
            )
        return dict(self.row)

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

    def test_immutability_trigger_still_allows_ownership_cascades(self):
        """Depth guard: direct mutation blocked, cascaded erasure permitted.

        Only the SQL text is asserted here. Whether Postgres actually raises on a
        direct UPDATE/DELETE and actually lets `DELETE FROM users` through is
        covered by the live-database suite.
        """
        text = MIGRATION_017.read_text(encoding="utf-8")
        self.assertIn("WHEN (pg_trigger_depth() = 0)", text)
        self.assertNotIn("DISABLE TRIGGER author_captures_no_update_delete", text)

    def test_composite_owner_keys_are_additive_to_the_existing_foreign_keys(self):
        text = MIGRATION_017.read_text(encoding="utf-8")
        self.assertIn("author_sessions_id_user_uidx UNIQUE (id, user_id)", text)
        for constraint in (
            "author_captures_session_user_fkey",
            "author_draft_versions_session_user_fkey",
            "author_flags_session_user_fkey",
        ):
            self.assertIn(constraint, text)
        self.assertEqual(
            text.count("REFERENCES author_sessions (id, user_id) ON DELETE CASCADE"), 3
        )
        # The single-column FKs the composite keys sit beside are untouched.
        self.assertEqual(text.count("REFERENCES author_sessions(id) ON DELETE CASCADE"), 3)
        self.assertEqual(text.count("REFERENCES public.users(id) ON DELETE CASCADE"), 5)

    def test_edit_decision_requires_replacement_text_in_schema(self):
        text = MIGRATION_017.read_text(encoding="utf-8")
        self.assertIn("decision <> 'edit' OR replacement_text IS NOT NULL", text)


class NoCaptureMutationSourceTests(unittest.TestCase):
    """Application layer: no UPDATE/DELETE statement targets author_captures.

    The invariant is repo-wide, so the scan is repo-wide: every shipped .py file,
    not just the two that happen to own the pipeline today. A capture write added
    from some future module has to fail here.
    """

    def test_no_module_anywhere_updates_or_deletes_captures(self):
        scanned = repo_python_sources()
        self.assertGreater(len(scanned), 20, "source scan found suspiciously few files")
        self.assertIn(REPO / "shared" / "author_pipeline" / "store.py", scanned)
        self.assertIn(REPO / "main.py", scanned)

        offenders: list[str] = []
        for path in scanned:
            lowered = path.read_text(encoding="utf-8").lower()
            for statement in ("update author_captures", "delete from author_captures"):
                if statement in lowered:
                    offenders.append(f"{path.relative_to(REPO)}: {statement}")
        self.assertEqual(offenders, [])

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

    # -- size limits ---------------------------------------------------------

    def test_capture_over_the_character_cap_is_rejected(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                path = f"/author/sessions/{sid}/captures"

                too_long = client.post(
                    path,
                    headers=h,
                    json={
                        "source": "voice",
                        "raw_text": "a" * (pipeline_store.MAX_CAPTURE_CHARS + 1),
                    },
                )
                self.assertEqual(too_long.status_code, 422, too_long.text[:300])

                at_the_cap = client.post(
                    path,
                    headers=h,
                    json={
                        "source": "voice",
                        "raw_text": "a" * pipeline_store.MAX_CAPTURE_CHARS,
                    },
                )
                self.assertEqual(at_the_cap.status_code, 200, at_the_cap.text[:300])

                stored = client.get(path, headers=h).json()
                self.assertEqual(stored["total"], 3)
                self.assertEqual(
                    len(stored["items"][2]["raw_text"]), pipeline_store.MAX_CAPTURE_CHARS
                )

    def test_service_caps_capture_length_even_when_the_router_is_bypassed(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = self._session_with_captures(client, h)
                user_id = pipeline_store._memory.sessions[sid]["user_id"]

                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(
                        pipeline_service.append_capture(
                            sid,
                            user_id,
                            source="voice",
                            raw_text="a" * (pipeline_store.MAX_CAPTURE_CHARS + 1),
                        )
                    )
                self.assertEqual(caught.exception.status_code, 413)

    def test_refine_range_over_the_prompt_budget_is_413_and_costs_nothing(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = client.post("/author/sessions", headers=h, json={}).json()["id"]

                chunk = "b" * pipeline_store.MAX_CAPTURE_CHARS
                chunks = (
                    pipeline_refine.MAX_REFINE_PROMPT_CHARS
                    // pipeline_store.MAX_CAPTURE_CHARS
                ) + 1
                for _ in range(chunks):
                    created = client.post(
                        f"/author/sessions/{sid}/captures",
                        headers=h,
                        json={"source": "voice", "raw_text": chunk},
                    )
                    self.assertEqual(created.status_code, 200, created.text[:300])

                whole_session = self._refine(client, h, sid)
                self.assertEqual(whole_session.status_code, 413, whole_session.text[:300])
                self.model.assert_not_called()

                narrower = self._refine(client, h, sid, {"capture_from": 0, "capture_to": 0})
                self.assertEqual(narrower.status_code, 200, narrower.text[:300])

                detail = client.get(f"/author/sessions/{sid}", headers=h).json()
                self.assertEqual(len(detail["draft_versions"]), 1)

    # -- concurrency ---------------------------------------------------------

    def test_unwinnable_capture_sequence_contention_is_a_retryable_503(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sid = client.post("/author/sessions", headers=h, json={}).json()["id"]

                with patch(
                    "shared.author_pipeline.store.append_capture",
                    new=AsyncMock(
                        side_effect=pipeline_store.CaptureSequenceContention("lost")
                    ),
                ):
                    response = client.post(
                        f"/author/sessions/{sid}/captures",
                        headers=h,
                        json={"source": "voice", "raw_text": RAW_ONE},
                    )
                self.assertEqual(response.status_code, 503, response.text[:300])
                self.assertIn("send this chunk again", response.json()["detail"])

    # -- soft references -----------------------------------------------------

    def test_session_creation_requires_owning_the_conversation_and_manuscript(self):
        with _env():
            with self._client() as client:
                token_a, token_b = self._two_users(client)
                ha, hb = self._auth(token_a), self._auth(token_b)
                bookkeeping_a = client.post("/author/sessions", headers=ha, json={}).json()["id"]
                bookkeeping_b = client.post("/author/sessions", headers=hb, json={}).json()["id"]
                user_a = pipeline_store._memory.sessions[bookkeeping_a]["user_id"]
                user_b = pipeline_store._memory.sessions[bookkeeping_b]["user_id"]

                convo_a = pipeline_store.memory_seed_conversation(user_a)
                convo_b = pipeline_store.memory_seed_conversation(user_b)
                manuscript_a = pipeline_store.memory_seed_manuscript(user_a)
                manuscript_b = pipeline_store.memory_seed_manuscript(user_b)

                owned = client.post(
                    "/author/sessions",
                    headers=ha,
                    json={"conversation_id": convo_a, "manuscript_id": manuscript_a},
                )
                self.assertEqual(owned.status_code, 200, owned.text[:300])
                self.assertEqual(owned.json()["conversation_id"], convo_a)
                self.assertEqual(owned.json()["manuscript_id"], manuscript_a)

                for body in (
                    {"conversation_id": convo_b},
                    {"conversation_id": str(uuid.uuid4())},
                    {"manuscript_id": manuscript_b},
                    {"manuscript_id": str(uuid.uuid4())},
                ):
                    blocked = client.post("/author/sessions", headers=ha, json=body)
                    self.assertEqual(blocked.status_code, 404, f"{body}: {blocked.text[:200]}")

                for body in (
                    {"conversation_id": "not-a-uuid"},
                    {"manuscript_id": "not-a-uuid"},
                ):
                    malformed = client.post("/author/sessions", headers=ha, json=body)
                    self.assertEqual(malformed.status_code, 400, malformed.text[:200])

                # Only the bookkeeping session and the validated one were stored.
                self.assertEqual(client.get("/author/sessions", headers=ha).json()["total"], 2)

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


class MalformedIdPathTests(unittest.TestCase):
    """A malformed path id takes the ordinary 404 path and never reaches SQL.

    Both author surfaces are covered: the capture pipeline and the older
    author_persistence routes, which carried the identical bug. They are asserted
    from this module because it is the test file this slice owns.

    The memory stores are switched OFF on purpose. That puts both stores on their
    Postgres branch against a pool where any query raises DataError, exactly as
    asyncpg does for a bad `$1::uuid` bind — so reaching SQL at all fails the test.
    """

    def setUp(self):
        use_memory_store(True)
        pipeline_store.use_memory_store(False)
        persistence_store.use_memory_store(False)
        self.pool = _NoSqlPool()
        for target in ("shared.db.init_pool", "shared.db.close_pool"):
            patcher = patch(target, new_callable=AsyncMock)
            patcher.start()
            self.addCleanup(patcher.stop)
        pool_patch = patch("shared.db.pool", return_value=self.pool)
        pool_patch.start()
        self.addCleanup(pool_patch.stop)
        self.addCleanup(lambda: use_memory_store(False))

    def _client(self):
        from main import app

        from routers.author_pipeline import router as pipeline_router

        if not any(getattr(r, "path", "") == "/author/sessions" for r in app.routes):
            app.include_router(pipeline_router)
        return TestClient(app)

    def _headers(self, client, username: str) -> dict:
        registered = client.post(
            "/auth/register",
            json={"username": username, "password": "password123"},  # pragma: allowlist secret
        )
        self.assertEqual(registered.status_code, 200, registered.text[:300])
        return {"Authorization": f"Bearer {registered.json()['access_token']}"}

    def _assert_all_404(self, client, headers, calls):
        for method, path, body in calls:
            request = getattr(client, method)
            response = request(path, headers=headers) if body is None else request(
                path, headers=headers, json=body
            )
            self.assertEqual(
                response.status_code, 404, f"{method.upper()} {path}: {response.text[:300]}"
            )
        self.assertEqual(self.pool.queries, [])

    def test_malformed_ids_on_every_pipeline_route_are_404(self):
        bad = "not-a-uuid"
        with _env():
            with self._client() as client:
                headers = self._headers(client, "malformed_pipeline")
                self._assert_all_404(
                    client,
                    headers,
                    [
                        ("get", f"/author/sessions/{bad}", None),
                        ("post", f"/author/sessions/{bad}/end", None),
                        ("get", f"/author/sessions/{bad}/captures", None),
                        (
                            "post",
                            f"/author/sessions/{bad}/captures",
                            {"source": "voice", "raw_text": RAW_ONE},
                        ),
                        ("post", f"/author/sessions/{bad}/refine", {}),
                        (
                            "post",
                            f"/author/flags/{bad}/decision",
                            {"decision": "reject"},
                        ),
                    ],
                )

    def test_malformed_ids_on_every_author_persistence_route_are_404(self):
        bad = "not-a-uuid"
        with _env():
            with self._client() as client:
                headers = self._headers(client, "malformed_persistence")
                self._assert_all_404(
                    client,
                    headers,
                    [
                        ("get", f"/author/projects/{bad}", None),
                        ("patch", f"/author/projects/{bad}", {"title": "renamed"}),
                        ("delete", f"/author/projects/{bad}", None),
                        ("get", f"/author/projects/{bad}/documents", None),
                        (
                            "post",
                            f"/author/projects/{bad}/documents",
                            {"title": "chapter"},
                        ),
                        ("get", f"/author/documents/{bad}", None),
                        (
                            "patch",
                            f"/author/documents/{bad}",
                            {"expected_revision": 1, "title": "renamed"},
                        ),
                        ("delete", f"/author/documents/{bad}", None),
                        ("post", f"/author/documents/{bad}/versions", None),
                        ("get", f"/author/documents/{bad}/versions", None),
                        ("get", f"/author/documents/{bad}/versions/{bad}", None),
                    ],
                )


class CaptureSequenceRetryTests(unittest.TestCase):
    """Concurrent appends lose the sequence race; that is retried, not a 500."""

    SESSION_ID = "6f1c2a0e-6a1d-4a2f-9a5e-2f3b7c8d9e01"
    USER_ID = "11111111-1111-4111-8111-111111111111"

    def setUp(self):
        pipeline_store.use_memory_store(False)
        now = datetime(2026, 8, 16, 18, 4, 19, tzinfo=timezone.utc)
        self.session = {"id": self.SESSION_ID, "user_id": self.USER_ID, "status": "active"}
        self.row = {
            "id": "b21f4c33-1d5e-4a77-8c90-9e2a1b3c4d55",
            "session_id": self.SESSION_ID,
            "user_id": self.USER_ID,
            "sequence": 7,
            "source": "voice",
            "raw_text": RAW_ONE,
            "captured_at": now,
            "created_at": now,
        }

    def _append(self, pool):
        with patch("shared.db.pool", return_value=pool):
            with patch(
                "shared.author_pipeline.store.get_session",
                new=AsyncMock(return_value=self.session),
            ):
                return asyncio.run(
                    pipeline_store.append_capture(
                        self.SESSION_ID, self.USER_ID, source="voice", raw_text=RAW_ONE
                    )
                )

    def test_a_lost_sequence_race_is_retried_until_it_wins(self):
        pool = _SequenceRacePool(failures=2, row=self.row)
        row = self._append(pool)
        self.assertEqual(pool.attempts, 3)
        self.assertEqual(row["sequence"], 7)
        self.assertEqual(row["raw_text"], RAW_ONE)

    def test_exhausted_retries_raise_the_typed_error_not_a_unique_violation(self):
        pool = _SequenceRacePool(failures=pipeline_store.CAPTURE_SEQUENCE_ATTEMPTS, row=self.row)
        with self.assertRaises(pipeline_store.CaptureSequenceContention):
            self._append(pool)
        self.assertEqual(pool.attempts, pipeline_store.CAPTURE_SEQUENCE_ATTEMPTS)


class RefinePromptBudgetTests(unittest.TestCase):
    """One /refine call has a hard ceiling on how much it can send upstream."""

    def _captures(self, count: int, chars: int) -> list[dict]:
        return [
            {"sequence": index, "source": "voice", "raw_text": "c" * chars}
            for index in range(count)
        ]

    def test_too_many_captures_is_413(self):
        captures = self._captures(pipeline_refine.MAX_REFINE_CAPTURES + 1, 1)
        prompt = pipeline_refine.build_user_prompt(captures)
        with self.assertRaises(HTTPException) as caught:
            pipeline_refine.assert_within_prompt_budget(captures, prompt)
        self.assertEqual(caught.exception.status_code, 413)
        self.assertIn(str(pipeline_refine.MAX_REFINE_CAPTURES), caught.exception.detail)

    def test_a_range_inside_both_ceilings_is_allowed(self):
        captures = self._captures(pipeline_refine.MAX_REFINE_CAPTURES, 10)
        prompt = pipeline_refine.build_user_prompt(captures)
        self.assertLess(len(prompt), pipeline_refine.MAX_REFINE_PROMPT_CHARS)
        pipeline_refine.assert_within_prompt_budget(captures, prompt)

    def test_an_oversized_prompt_never_reaches_the_model(self):
        captures = self._captures(2, pipeline_refine.MAX_REFINE_PROMPT_CHARS)
        model = Mock(return_value=_model_reply())
        with patch("shared.author_pipeline.refine.call_model", model):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(pipeline_refine.refine_captures(captures, "preserve_voice"))
        self.assertEqual(caught.exception.status_code, 413)
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
