"""SQL-store isolation: application functions must filter by user_id.

The in-memory fakes and the raw-SQL live invariants would still pass if a
store dropped `AND user_id = $N`. These tests call the real async store
functions against a scratch Postgres and fail if that predicate disappears.

Gated like the live invariants: RUN_LIVE_DB_INVARIANTS=1 and DATABASE_URL.
Uses its own scratch database so it cannot disturb migration-repro or the
live-invariants scratch DB.

Run:  RUN_LIVE_DB_INVARIANTS=1 DATABASE_URL=... python -m unittest \
          tests.test_sql_store_isolation -v
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from shared.health.types import NormalizedSample

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "migrations"
SCRATCH_DB = "lifesight_sql_store_isolation"

USER_A = "aaaaaaaa-0000-4000-8000-000000000001"
USER_B = "bbbbbbbb-0000-4000-8000-000000000002"


def _enabled() -> bool:
    return os.environ.get("RUN_LIVE_DB_INVARIANTS") == "1" and bool(
        os.environ.get("DATABASE_URL")
    )


def _swap_database(dsn: str, database: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment)
    )


def _sample(external_id: str, value: float) -> NormalizedSample:
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    return NormalizedSample(
        external_id=external_id,
        sample_type="heart_rate",
        start_at=when,
        end_at=when,
        value=value,
        unit="count/min",
        value_text=None,
        source_bundle="com.apple.health",
        source_name="Test Watch",
        metadata={},
    )


@unittest.skipUnless(
    _enabled(), "set RUN_LIVE_DB_INVARIANTS=1 and DATABASE_URL to run"
)
class SqlStoreIsolationTests(unittest.TestCase):
    """Each test goes through store.py, not hand-written SELECT ... user_id."""

    _loop = None
    _admin_dsn = None
    _scratch_dsn = None

    @classmethod
    def setUpClass(cls):
        import asyncpg

        from shared import db
        from shared.author_pipeline import store as author_store
        from shared.author_persistence import store as persistence_store
        from shared.health import store as health_store
        from shared.personalization import store as personal_store

        cls.asyncpg = asyncpg
        cls.db = db
        cls.author_store = author_store
        cls.health_store = health_store
        cls.personal_store = personal_store
        cls._loop = asyncio.new_event_loop()
        cls._admin_dsn = os.environ["DATABASE_URL"]
        cls._scratch_dsn = _swap_database(cls._admin_dsn, SCRATCH_DB)

        async def build():
            admin = await asyncpg.connect(
                cls._admin_dsn, statement_cache_size=0, timeout=60
            )
            try:
                await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
                await admin.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
            finally:
                await admin.close()

            conn = await asyncpg.connect(
                cls._scratch_dsn, statement_cache_size=0, timeout=60
            )
            try:
                await conn.execute("CREATE SCHEMA IF NOT EXISTS auth")
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS auth.users (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid()
                    )
                    """
                )
                for sql_file in sorted(MIGRATIONS.glob("*.sql")):
                    await conn.execute(sql_file.read_text(encoding="utf-8"))
                for user_id, name in ((USER_A, "iso_probe_a"), (USER_B, "iso_probe_b")):
                    await conn.execute(
                        """
                        INSERT INTO users (id, username, password_hash, is_active)
                        VALUES ($1::uuid, $2, 'x', TRUE)
                        ON CONFLICT DO NOTHING
                        """,
                        user_id,
                        name,
                    )
            finally:
                await conn.close()

            os.environ["DATABASE_URL"] = cls._scratch_dsn
            health_store.use_memory_store(False)
            author_store.use_memory_store(False)
            persistence_store.use_memory_store(False)
            personal_store.use_memory_store(False)
            await db.close_pool()
            await db.init_pool()
            db.pool()

        cls._loop.run_until_complete(build())

    @classmethod
    def tearDownClass(cls):
        async def teardown():
            await cls.db.close_pool()
            os.environ["DATABASE_URL"] = cls._admin_dsn
            admin = await cls.asyncpg.connect(
                cls._admin_dsn, statement_cache_size=0, timeout=60
            )
            try:
                await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
            finally:
                await admin.close()

        cls._loop.run_until_complete(teardown())
        cls._loop.close()

    def run_async(self, coro):
        return self.__class__._loop.run_until_complete(coro)

    # -- health ----------------------------------------------------------

    def test_health_store_does_not_leak_or_overwrite_across_users(self):
        async def go():
            inserted, _updated, _unchanged = await self.health_store.upsert_samples(
                USER_A, "healthkit", [_sample("shared-id", 55.0)]
            )
            self.assertEqual(inserted, 1)
            await self.health_store.touch_sync_state(USER_A)

            since = datetime.now(timezone.utc) - timedelta(days=7)
            status_b = await self.health_store.category_status(USER_B, since=since)
            self.assertEqual(status_b, {})
            rollup_b = await self.health_store.daily_rollup(
                USER_B, ["heart_rate"], since=since
            )
            self.assertEqual(rollup_b, [])
            latest_b = await self.health_store.latest_per_type(
                USER_B, ["heart_rate"], since=since
            )
            self.assertEqual(latest_b, {})
            self.assertIsNone(await self.health_store.get_last_synced_at(USER_B))

            inserted_b, updated_b, _ = await self.health_store.upsert_samples(
                USER_B, "healthkit", [_sample("shared-id", 99.0)]
            )
            self.assertEqual(inserted_b, 1)
            self.assertEqual(updated_b, 0)

            status_a = await self.health_store.category_status(USER_A, since=since)
            status_b = await self.health_store.category_status(USER_B, since=since)
            self.assertEqual(status_a["heart_rate"]["count_recent"], 1)
            self.assertEqual(status_b["heart_rate"]["count_recent"], 1)

            latest_a = await self.health_store.latest_per_type(
                USER_A, ["heart_rate"], since=since
            )
            latest_b = await self.health_store.latest_per_type(
                USER_B, ["heart_rate"], since=since
            )
            self.assertEqual(latest_a["heart_rate"]["value"], 55.0)
            self.assertEqual(latest_b["heart_rate"]["value"], 99.0)

            rows = await self.db.pool().fetch(
                """
                SELECT user_id::text, value FROM health_samples
                WHERE provider = 'healthkit' AND external_id = 'shared-id'
                ORDER BY user_id
                """
            )
            by_user = {r["user_id"]: float(r["value"]) for r in rows}
            self.assertEqual(by_user[USER_A], 55.0)
            self.assertEqual(by_user[USER_B], 99.0)

        self.run_async(go())

    # -- author pipeline -------------------------------------------------

    def test_author_store_reads_are_owner_scoped_and_malformed_ids_are_none(self):
        async def go():
            session = await self.author_store.create_session(USER_A, title="A sitting")
            session_id = str(session["id"])
            capture = await self.author_store.append_capture(
                session_id, USER_A, source="voice", raw_text="the words as dictated"
            )
            self.assertIsNotNone(capture)
            version, flags = await self.author_store.create_refinement(
                session_id,
                USER_A,
                refinement_level="preserve_voice",
                content="the words as dictated",
                source_capture_from=0,
                source_capture_to=0,
                model_identifier="test-model",
                flags=[
                    {
                        "category": "typo",
                        "explanation": "a possible typo",
                        "suggested_change": None,
                    }
                ],
            )
            self.assertIsNone(await self.author_store.get_session(session_id, USER_B))
            self.assertIsNone(await self.author_store.list_captures(session_id, USER_B))
            self.assertIsNone(await self.author_store.get_flag(str(flags[0]["id"]), USER_B))
            self.assertIsNone(
                await self.author_store.get_draft_version(str(version["id"]), USER_B)
            )

            self.assertIsNone(await self.author_store.get_session("not-a-uuid", USER_A))
            try:
                leaked = await self.author_store.get_session("not-a-uuid", USER_A)
            except self.asyncpg.exceptions.DataError:
                self.fail("malformed UUID must not reach a $1::uuid bind")
            self.assertIsNone(leaked)

        self.run_async(go())

    # -- personalization -------------------------------------------------

    def test_personalization_store_does_not_leak_across_users(self):
        async def go():
            period_start = date(2026, 8, 10)
            period_end = date(2026, 8, 10)
            summary = await self.personal_store.upsert_summary(
                USER_A,
                scope="daily",
                period_start=period_start,
                period_end=period_end,
                summary="A's day",
                source_conversation_ids=[],
                source_summary_ids=[],
                model_identifier="test-model",
            )
            proposal = await self.personal_store.insert_pending_proposal(
                USER_A,
                mode="fitness",
                proposed_instructions="try this",
                reasoning="because the week looked like this",
                evidence={},
                risks=None,
                model_identifier="test-model",
            )

            self.assertIsNone(
                await self.personal_store.get_summary(
                    USER_B,
                    scope="daily",
                    period_start=period_start,
                    period_end=period_end,
                )
            )
            self.assertEqual(
                await self.personal_store.list_summaries(
                    USER_B,
                    scopes=("daily",),
                    period_start=period_start,
                    period_end=period_end,
                ),
                [],
            )
            self.assertIsNone(
                await self.personal_store.get_proposal(str(proposal["id"]), USER_B)
            )
            self.assertEqual(
                await self.personal_store.list_pending_proposals(USER_B), []
            )

            owned = await self.personal_store.get_summary(
                USER_A,
                scope="daily",
                period_start=period_start,
                period_end=period_end,
            )
            self.assertEqual(str(owned["id"]), str(summary["id"]))

            with self.assertRaises(self.personal_store.PendingProposalExistsError):
                await self.personal_store.insert_pending_proposal(
                    USER_A,
                    mode="fitness",
                    proposed_instructions="a second pending proposal",
                    reasoning="should be rejected by the unique index",
                    evidence={},
                    risks=None,
                    model_identifier="test-model",
                )

        self.run_async(go())


if __name__ == "__main__":
    unittest.main()
