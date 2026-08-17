"""Fitness workout V1 — live/disposable Postgres gate tests.

Exercises migration 019 dedupe, ownership FKs, store isolation, and
concurrent session starts against real Postgres (not in-memory fakes).

Run:
    RUN_LIVE_DB_INVARIANTS=1 DATABASE_URL=postgresql://... \\
        python -m unittest tests.test_fitness_live_db_v1 -v
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
AUTH_STUB = REPO / "scripts" / "ci_prepare_auth_schema.sql"
MIGRATION_019 = MIGRATIONS / "019_fitness_workout_v1.sql"

SCRATCH_DB = "lifesight_fitness_live_v1"
UPGRADE_DB = "lifesight_fitness_019_upgrade"

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


def _migration_files(max_number: int | None = None) -> list[Path]:
    files: list[Path] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        prefix = path.name.split("_", 1)[0]
        if not prefix.isdigit():
            continue
        num = int(prefix)
        if max_number is not None and num > max_number:
            continue
        files.append(path)
    return files


async def _create_scratch(admin_dsn: str, database: str) -> str:
    import asyncpg

    scratch_dsn = _swap_database(admin_dsn, database)
    admin = await asyncpg.connect(admin_dsn, statement_cache_size=0, timeout=60)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    return scratch_dsn


async def _apply_migrations(conn, *, max_number: int | None = None) -> None:
    await conn.execute("CREATE SCHEMA IF NOT EXISTS auth")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth.users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid()
        )
        """
    )
    if AUTH_STUB.is_file():
        await conn.execute(AUTH_STUB.read_text(encoding="utf-8"))
    for sql_file in _migration_files(max_number):
        await conn.execute(sql_file.read_text(encoding="utf-8"))


async def _seed_users(conn) -> None:
    for user_id, name in ((USER_A, "fitness_live_a"), (USER_B, "fitness_live_b")):
        await conn.execute(
            """
            INSERT INTO users (id, username, password_hash, is_active)
            VALUES ($1::uuid, $2, 'x', TRUE)
            ON CONFLICT DO NOTHING
            """,
            user_id,
            name,
        )


@unittest.skipUnless(
    _enabled(), "set RUN_LIVE_DB_INVARIANTS=1 and DATABASE_URL to run"
)
class FitnessMigration019UpgradeTests(unittest.TestCase):
    """Apply 001–018, seed legacy duplicates, then 019 dedupes before indexes."""

    _loop = None
    _admin_dsn = None
    conn = None

    @classmethod
    def setUpClass(cls):
        import asyncpg

        cls.asyncpg = asyncpg
        cls._loop = asyncio.new_event_loop()
        cls._admin_dsn = os.environ["DATABASE_URL"]

        async def build():
            scratch_dsn = await _create_scratch(cls._admin_dsn, UPGRADE_DB)
            conn = await asyncpg.connect(scratch_dsn, statement_cache_size=0, timeout=60)
            await _apply_migrations(conn, max_number=18)
            await _seed_users(conn)

            plan_id = await conn.fetchval(
                """
                INSERT INTO workout_plans (user_id)
                VALUES ($1::uuid)
                RETURNING id
                """,
                USER_A,
            )
            day_id = await conn.fetchval(
                """
                INSERT INTO workout_days (plan_id, sort_order, title)
                VALUES ($1::uuid, 0, 'Day A')
                RETURNING id
                """,
                plan_id,
            )
            exercise_id = await conn.fetchval(
                """
                INSERT INTO planned_exercises
                    (day_id, name, target_sets, target_reps, rest_seconds, sort_order)
                VALUES ($1::uuid, 'Bench', 3, 5, 90, 0)
                RETURNING id
                """,
                day_id,
            )
            older = await conn.fetchval(
                """
                INSERT INTO workout_sessions (user_id, plan_day_id, status, started_at)
                VALUES ($1::uuid, $2::uuid, 'active', now() - interval '2 hours')
                RETURNING id
                """,
                USER_A,
                day_id,
            )
            newer = await conn.fetchval(
                """
                INSERT INTO workout_sessions (user_id, plan_day_id, status, started_at)
                VALUES ($1::uuid, $2::uuid, 'active', now() - interval '1 hour')
                RETURNING id
                """,
                USER_A,
                day_id,
            )
            await conn.execute(
                """
                INSERT INTO set_logs
                    (session_id, exercise_id, set_number, reps, weight, source)
                VALUES ($1::uuid, $2::uuid, 1, 5, 100, 'manual')
                """,
                newer,
                exercise_id,
            )
            await conn.execute(
                """
                INSERT INTO set_logs
                    (session_id, exercise_id, set_number, reps, weight, source)
                VALUES ($1::uuid, $2::uuid, 1, 5, 110, 'manual')
                """,
                newer,
                exercise_id,
            )
            cls._legacy = {
                "plan_id": plan_id,
                "day_id": day_id,
                "exercise_id": exercise_id,
                "older_session": older,
                "newer_session": newer,
            }
            await conn.execute(MIGRATION_019.read_text(encoding="utf-8"))
            return conn

        cls.conn = cls._loop.run_until_complete(build())

    @classmethod
    def tearDownClass(cls):
        async def teardown():
            if cls.conn is not None:
                await cls.conn.close()
            admin = await cls.asyncpg.connect(
                cls._admin_dsn, statement_cache_size=0, timeout=60
            )
            try:
                await admin.execute(f'DROP DATABASE IF EXISTS "{UPGRADE_DB}"')
            finally:
                await admin.close()

        cls._loop.run_until_complete(teardown())
        cls._loop.close()

    def run_async(self, coro):
        return self.__class__._loop.run_until_complete(coro)

    def test_duplicate_active_sessions_are_deduped(self):
        async def go():
            active = await self.conn.fetch(
                """
                SELECT id, status FROM workout_sessions
                WHERE user_id = $1::uuid AND status = 'active'
                ORDER BY started_at DESC
                """,
                USER_A,
            )
            self.assertEqual(len(active), 1)
            self.assertEqual(str(active[0]["id"]), str(self._legacy["newer_session"]))
            abandoned = await self.conn.fetchval(
                """
                SELECT status FROM workout_sessions WHERE id = $1::uuid
                """,
                self._legacy["older_session"],
            )
            self.assertEqual(abandoned, "abandoned")

        self.run_async(go())

    def test_duplicate_set_logs_are_deduped(self):
        async def go():
            rows = await self.conn.fetch(
                """
                SELECT id, weight FROM set_logs
                WHERE session_id = $1::uuid AND exercise_id = $2::uuid AND set_number = 1
                """,
                self._legacy["newer_session"],
                self._legacy["exercise_id"],
            )
            self.assertEqual(len(rows), 1)

        self.run_async(go())

    def test_unique_indexes_exist(self):
        async def go():
            names = {
                r["indexname"]
                for r in await self.conn.fetch(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND indexname IN (
                        'workout_sessions_one_active_per_user',
                        'set_logs_session_exercise_set_uidx',
                        'workout_plans_one_active_per_user'
                      )
                    """
                )
            }
            self.assertIn("workout_sessions_one_active_per_user", names)
            self.assertIn("set_logs_session_exercise_set_uidx", names)
            self.assertIn("workout_plans_one_active_per_user", names)

        self.run_async(go())

    def test_cross_user_set_log_is_rejected(self):
        async def go():
            with self.assertRaises(self.asyncpg.exceptions.ForeignKeyViolationError):
                await self.conn.execute(
                    """
                    INSERT INTO set_logs
                        (user_id, session_id, exercise_id, set_number, reps, weight, source)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, 2, 5, 50, 'manual')
                    """,
                    USER_B,
                    self._legacy["newer_session"],
                    self._legacy["exercise_id"],
                )

        self.run_async(go())


@unittest.skipUnless(
    _enabled(), "set RUN_LIVE_DB_INVARIANTS=1 and DATABASE_URL to run"
)
class FitnessStoreLiveGateTests(unittest.TestCase):
    """Store-layer isolation and concurrent starts on a fully migrated scratch DB."""

    _loop = None
    _admin_dsn = None

    @classmethod
    def setUpClass(cls):
        import asyncpg

        from shared import db
        from shared.fitness import service, store

        cls.asyncpg = asyncpg
        cls.db = db
        cls.store = store
        cls.service = service
        cls._loop = asyncio.new_event_loop()
        cls._admin_dsn = os.environ["DATABASE_URL"]
        cls._scratch_dsn = None

        async def build():
            cls._scratch_dsn = await _create_scratch(cls._admin_dsn, SCRATCH_DB)
            conn = await asyncpg.connect(
                cls._scratch_dsn, statement_cache_size=0, timeout=60
            )
            try:
                await _apply_migrations(conn)
                await _seed_users(conn)
            finally:
                await conn.close()
            os.environ["DATABASE_URL"] = cls._scratch_dsn
            store.use_memory_store(False)
            await db.close_pool()
            await db.init_pool()

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

    def setUp(self):
        async def clean():
            for user_id in (USER_A, USER_B):
                await self.db.pool().execute(
                    "DELETE FROM set_logs WHERE user_id = $1::uuid", user_id
                )
                await self.db.pool().execute(
                    "DELETE FROM personal_records WHERE user_id = $1::uuid", user_id
                )
                await self.db.pool().execute(
                    "DELETE FROM workout_sessions WHERE user_id = $1::uuid", user_id
                )
                await self.db.pool().execute(
                    "DELETE FROM planned_exercises WHERE user_id = $1::uuid", user_id
                )
                await self.db.pool().execute(
                    "DELETE FROM workout_days WHERE user_id = $1::uuid", user_id
                )
                await self.db.pool().execute(
                    "DELETE FROM workout_plans WHERE user_id = $1::uuid", user_id
                )

        self.run_async(clean())

    async def _plan_for(self, user_id: str) -> dict:
        row = await self.store.create_plan(
            user_id,
            title="Gate plan",
            notes=None,
            days=[
                {
                    "title": "Day A",
                    "sort_order": 0,
                    "exercises": [
                        {
                            "name": "Bench Press",
                            "target_sets": 3,
                            "target_reps": 5,
                            "rest_seconds": 90,
                            "sort_order": 0,
                        }
                    ],
                }
            ],
            activate=True,
        )
        return await self.store.assemble_plan(str(row["id"]), user_id)

    def test_concurrent_start_has_one_active_session(self):
        async def go():
            plan = await self._plan_for(USER_A)
            day_id = plan["days"][0]["id"]

            async def start_once():
                return await self.store.start_or_resume_session(USER_A, day_id)

            results = await asyncio.gather(start_once(), start_once())
            sessions = {str(r[0]["id"]) for r in results}
            self.assertEqual(len(sessions), 1)
            active = await self.store.get_active_session(USER_A)
            self.assertIsNotNone(active)
            self.assertEqual(str(active["id"]), next(iter(sessions)))
            count = await self.db.pool().fetchval(
                """
                SELECT COUNT(*) FROM workout_sessions
                WHERE user_id = $1::uuid AND status = 'active'
                """,
                USER_A,
            )
            self.assertEqual(int(count), 1)

        self.run_async(go())

    def test_cross_user_store_paths_do_not_leak(self):
        async def go():
            plan_a = await self._plan_for(USER_A)
            day_a = plan_a["days"][0]["id"]
            ex_a = plan_a["days"][0]["exercises"][0]["id"]
            session_a, _ = await self.store.start_or_resume_session(USER_A, day_a)
            sid = str(session_a["id"])
            await self.store.insert_set_log(
                USER_A, sid, ex_a, 1, 5, 135.0, source="manual"
            )
            await self.store.upsert_personal_record(USER_A, ex_a, 5, 135.0)

            self.assertIsNone(await self.store.get_plan(str(plan_a["id"]), USER_B))
            self.assertIsNone(await self.store.get_session(sid, USER_B))
            self.assertIsNone(await self.store.get_exercise(ex_a, USER_B))
            self.assertEqual(await self.store.list_set_logs(sid, USER_B), [])
            self.assertEqual(
                await self.store.list_exercise_history(USER_B, ex_a, limit=10), []
            )
            self.assertEqual(
                await self.store.list_personal_records(USER_B, limit=10), []
            )

            with self.assertRaises(self.asyncpg.exceptions.ForeignKeyViolationError):
                await self.store.insert_set_log(
                    USER_B, sid, ex_a, 2, 5, 200.0, source="manual"
                )

            self.assertIsNone(await self.store.complete_session(sid, USER_B))
            still = await self.store.get_session(sid, USER_A)
            self.assertIsNotNone(still)
            self.assertEqual(still["status"], "active")

        self.run_async(go())

    def test_user_delete_cascades_workout_rows(self):
        async def go():
            doomed = str(uuid.uuid4())
            await self.db.pool().execute(
                """
                INSERT INTO users (id, username, password_hash, is_active)
                VALUES ($1::uuid, $2, 'x', TRUE)
                """,
                doomed,
                f"probe_{doomed[:8]}",
            )
            plan = await self._plan_for(doomed)
            day_id = plan["days"][0]["id"]
            ex_id = plan["days"][0]["exercises"][0]["id"]
            session, _ = await self.store.start_or_resume_session(doomed, day_id)
            sid = str(session["id"])
            await self.store.insert_set_log(
                doomed, sid, ex_id, 1, 5, 100.0, source="manual"
            )

            await self.db.pool().execute(
                "DELETE FROM users WHERE id = $1::uuid", doomed
            )
            for table, column in (
                ("workout_plans", "user_id"),
                ("workout_sessions", "user_id"),
                ("set_logs", "user_id"),
            ):
                remaining = await self.db.pool().fetchval(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} = $1::uuid", doomed
                )
                self.assertEqual(int(remaining), 0, table)

        self.run_async(go())


if __name__ == "__main__":
    unittest.main()
