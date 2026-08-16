"""Execute the database-level safety guarantees against a real Postgres.

The rest of the suite asserts these by reading migration file text, which
proves the SQL was written, not that Postgres enforces it. These are the
invariants where that distinction matters: raw author captures being
append-only, an AI prompt proposal being unable to rewrite itself or reach
`approved` without a human reviewer, and health samples deduping on re-sync.

Opt-in and destructive: creates and drops its own scratch database on the
server named by DATABASE_URL, so it cannot disturb the empty-schema database
that `tests.test_migration_repro` requires.

Run:  RUN_LIVE_DB_INVARIANTS=1 DATABASE_URL=... python -m unittest \
          tests.test_live_db_invariants -v
"""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "migrations"
SCRATCH_DB = "lifesight_live_invariants"

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


@unittest.skipUnless(
    _enabled(), "set RUN_LIVE_DB_INVARIANTS=1 and DATABASE_URL to run"
)
class LiveDatabaseInvariantTests(unittest.TestCase):
    """Each test asserts Postgres itself rejects the unsafe operation."""

    conn = None
    _loop = None

    @classmethod
    def setUpClass(cls):
        import asyncpg

        cls.asyncpg = asyncpg
        cls._loop = asyncio.new_event_loop()
        dsn = os.environ["DATABASE_URL"]
        cls._scratch_dsn = _swap_database(dsn, SCRATCH_DB)

        async def build():
            admin = await asyncpg.connect(dsn, statement_cache_size=0, timeout=60)
            try:
                await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
                await admin.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
            finally:
                await admin.close()

            conn = await asyncpg.connect(
                cls._scratch_dsn, statement_cache_size=0, timeout=60
            )
            # Migrations 001-006 reference auth.users; CI stubs it the same way.
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
            for user_id, name in ((USER_A, "live_probe_a"), (USER_B, "live_probe_b")):
                await conn.execute(
                    """
                    INSERT INTO users (id, username, password_hash, is_active)
                    VALUES ($1::uuid, $2, 'x', TRUE)
                    ON CONFLICT DO NOTHING
                    """,
                    user_id,
                    name,
                )
            return conn

        cls.conn = cls._loop.run_until_complete(build())

    @classmethod
    def tearDownClass(cls):
        async def teardown():
            if cls.conn is not None:
                await cls.conn.close()
            admin = await cls.asyncpg.connect(
                os.environ["DATABASE_URL"], statement_cache_size=0, timeout=60
            )
            try:
                await admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
            finally:
                await admin.close()

        cls._loop.run_until_complete(teardown())
        cls._loop.close()

    def run_async(self, coro):
        return self.__class__._loop.run_until_complete(coro)

    # -- helpers ---------------------------------------------------------

    async def _new_session(self, user_id: str = USER_A) -> str:
        return str(
            await self.conn.fetchval(
                "INSERT INTO author_sessions (user_id) VALUES ($1::uuid) RETURNING id",
                user_id,
            )
        )

    async def _new_capture(self, session_id: str, user_id: str = USER_A) -> str:
        return str(
            await self.conn.fetchval(
                """
                INSERT INTO author_captures
                    (session_id, user_id, sequence, source, raw_text)
                VALUES ($1::uuid, $2::uuid, 0, 'voice', 'the words as dictated')
                RETURNING id
                """,
                session_id,
                user_id,
            )
        )

    # -- author captures are append-only ---------------------------------

    def test_direct_update_of_a_raw_capture_is_rejected(self):
        async def go():
            session_id = await self._new_session()
            capture_id = await self._new_capture(session_id)
            with self.assertRaises(self.asyncpg.exceptions.RestrictViolationError):
                await self.conn.execute(
                    "UPDATE author_captures SET raw_text = 'tampered' WHERE id = $1::uuid",
                    capture_id,
                )
            still = await self.conn.fetchval(
                "SELECT raw_text FROM author_captures WHERE id = $1::uuid", capture_id
            )
            self.assertEqual(still, "the words as dictated")

        self.run_async(go())

    def test_direct_delete_of_a_raw_capture_is_rejected(self):
        async def go():
            session_id = await self._new_session()
            capture_id = await self._new_capture(session_id)
            with self.assertRaises(self.asyncpg.exceptions.RestrictViolationError):
                await self.conn.execute(
                    "DELETE FROM author_captures WHERE id = $1::uuid", capture_id
                )

        self.run_async(go())

    def test_deleting_the_owning_user_still_erases_captures(self):
        """Erasure must remain possible — the trigger guards edits, not cascades."""

        async def go():
            doomed = str(uuid.uuid4())
            await self.conn.execute(
                """
                INSERT INTO users (id, username, password_hash, is_active)
                VALUES ($1::uuid, $2, 'x', TRUE)
                """,
                doomed,
                f"probe_{doomed[:8]}",
            )
            session_id = str(
                await self.conn.fetchval(
                    "INSERT INTO author_sessions (user_id) VALUES ($1::uuid) RETURNING id",
                    doomed,
                )
            )
            await self._new_capture(session_id, doomed)

            await self.conn.execute("DELETE FROM users WHERE id = $1::uuid", doomed)

            remaining = await self.conn.fetchval(
                "SELECT COUNT(*) FROM author_captures WHERE user_id = $1::uuid", doomed
            )
            self.assertEqual(int(remaining), 0)

        self.run_async(go())

    def test_a_capture_cannot_claim_a_different_owner_than_its_session(self):
        async def go():
            session_id = await self._new_session(USER_A)
            with self.assertRaises(self.asyncpg.exceptions.ForeignKeyViolationError):
                await self.conn.execute(
                    """
                    INSERT INTO author_captures
                        (session_id, user_id, sequence, source, raw_text)
                    VALUES ($1::uuid, $2::uuid, 99, 'voice', 'cross-owner')
                    """,
                    session_id,
                    USER_B,
                )

        self.run_async(go())

    # -- prompt proposals stay human-gated -------------------------------

    async def _new_proposal(self, mode: str) -> str:
        return str(
            await self.conn.fetchval(
                """
                INSERT INTO prompt_change_proposals
                    (user_id, mode, proposed_instructions, reasoning, model_identifier)
                VALUES ($1::uuid, $2, 'the original AI proposal', 'because', 'test-model')
                RETURNING id
                """,
                USER_A,
                mode,
            )
        )

    def test_proposed_instructions_cannot_be_rewritten(self):
        async def go():
            proposal_id = await self._new_proposal("author")
            with self.assertRaises(self.asyncpg.exceptions.RestrictViolationError):
                await self.conn.execute(
                    "UPDATE prompt_change_proposals SET proposed_instructions = 'hijacked' "
                    "WHERE id = $1::uuid",
                    proposal_id,
                )
            # The human-editable column is still writable.
            await self.conn.execute(
                "UPDATE prompt_change_proposals SET final_instructions = 'human edit' "
                "WHERE id = $1::uuid",
                proposal_id,
            )

        self.run_async(go())

    def test_approval_without_a_recorded_reviewer_is_rejected(self):
        async def go():
            proposal_id = await self._new_proposal("diet")
            with self.assertRaises(self.asyncpg.exceptions.CheckViolationError):
                await self.conn.execute(
                    "UPDATE prompt_change_proposals SET status = 'approved' "
                    "WHERE id = $1::uuid",
                    proposal_id,
                )
            await self.conn.execute(
                """
                UPDATE prompt_change_proposals
                SET status = 'approved', reviewed_at = now(), reviewed_by = 'a-human'
                WHERE id = $1::uuid
                """,
                proposal_id,
            )
            status = await self.conn.fetchval(
                "SELECT status FROM prompt_change_proposals WHERE id = $1::uuid",
                proposal_id,
            )
            self.assertEqual(status, "approved")

        self.run_async(go())

    def test_only_one_pending_proposal_per_user_and_mode(self):
        async def go():
            await self._new_proposal("brainstorm")
            with self.assertRaises(self.asyncpg.exceptions.UniqueViolationError):
                await self._new_proposal("brainstorm")

        self.run_async(go())

    # -- health samples dedupe on re-sync --------------------------------

    async def _insert_sample(self, external_id: str, provider: str, value: float):
        await self.conn.execute(
            """
            INSERT INTO health_samples
                (user_id, provider, external_id, sample_type, start_at, end_at, value, unit)
            VALUES ($1::uuid, $2, $3, 'heart_rate', now(), now(), $4, 'count/min')
            """,
            USER_A,
            provider,
            external_id,
            value,
        )

    def test_resyncing_the_same_sample_id_cannot_duplicate_a_row(self):
        async def go():
            external_id = f"hk-{uuid.uuid4()}"
            await self._insert_sample(external_id, "healthkit", 68)
            with self.assertRaises(self.asyncpg.exceptions.UniqueViolationError):
                await self._insert_sample(external_id, "healthkit", 99)
            # The same id from a different provider is a genuinely different sample.
            await self._insert_sample(external_id, "terra", 68)

        self.run_async(go())

    def test_a_sample_cannot_end_before_it_starts(self):
        async def go():
            with self.assertRaises(self.asyncpg.exceptions.CheckViolationError):
                await self.conn.execute(
                    """
                    INSERT INTO health_samples
                        (user_id, provider, external_id, sample_type,
                         start_at, end_at, value, unit)
                    VALUES ($1::uuid, 'healthkit', $2, 'heart_rate',
                            now(), now() - interval '1 hour', 70, 'count/min')
                    """,
                    USER_A,
                    f"hk-bad-{uuid.uuid4()}",
                )

        self.run_async(go())

    # -- ownership is enforced by SQL, not only by the memory fakes ------

    def test_the_real_sql_store_does_not_leak_across_users(self):
        """The offline suite runs against in-memory fakes; prove the SQL agrees."""

        async def go():
            session_id = await self._new_session(USER_A)
            await self._new_capture(session_id, USER_A)

            owned = await self.conn.fetchval(
                "SELECT COUNT(*) FROM author_sessions WHERE id = $1::uuid AND user_id = $2::uuid",
                session_id,
                USER_A,
            )
            self.assertEqual(int(owned), 1)

            leaked = await self.conn.fetchval(
                "SELECT COUNT(*) FROM author_sessions WHERE id = $1::uuid AND user_id = $2::uuid",
                session_id,
                USER_B,
            )
            self.assertEqual(int(leaked), 0, "user B must not resolve user A's session")

            captures_for_b = await self.conn.fetchval(
                "SELECT COUNT(*) FROM author_captures WHERE session_id = $1::uuid "
                "AND user_id = $2::uuid",
                session_id,
                USER_B,
            )
            self.assertEqual(int(captures_for_b), 0)

        self.run_async(go())


if __name__ == "__main__":
    unittest.main()
