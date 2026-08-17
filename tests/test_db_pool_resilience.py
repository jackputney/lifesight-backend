"""Database pool resilience — health/db, recovery, sanitized 503.

Run:  python -m unittest tests.test_db_pool_resilience -v
"""

from __future__ import annotations

import os
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import asyncpg
from fastapi.testclient import TestClient

from shared import db
from shared.local_auth.store import use_memory_store


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "dev",
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class _FakeRawPool:
    """Minimal asyncpg.Pool stand-in for resilience tests."""

    def __init__(self, *, fetchval=None, fetchrow=None, execute=None, acquire_exc=None):
        self._fetchval = fetchval
        self._fetchrow = fetchrow
        self._execute = execute
        self._acquire_exc = acquire_exc
        self.fetchval_calls = 0
        self.fetchrow_calls = 0
        self.execute_calls = 0
        self.closed = False
        self._size = 1
        self._idle = 1

    def get_size(self):
        return self._size

    def get_idle_size(self):
        return self._idle

    def get_max_size(self):
        return 5

    def get_min_size(self):
        return 1

    async def close(self):
        self.closed = True

    async def fetchval(self, query, *args, **kwargs):
        self.fetchval_calls += 1
        if isinstance(self._fetchval, Exception):
            raise self._fetchval
        if callable(self._fetchval):
            return self._fetchval(self, query, *args)
        return self._fetchval

    async def fetchrow(self, query, *args, **kwargs):
        self.fetchrow_calls += 1
        if isinstance(self._fetchrow, Exception):
            raise self._fetchrow
        if callable(self._fetchrow):
            return self._fetchrow(self, query, *args)
        return self._fetchrow

    async def execute(self, query, *args, **kwargs):
        self.execute_calls += 1
        if isinstance(self._execute, Exception):
            raise self._execute
        if callable(self._execute):
            return self._execute(self, query, *args)
        return self._execute or "OK"

    def acquire(self, *, timeout=None):
        @asynccontextmanager
        async def _cm():
            if self._acquire_exc is not None:
                raise self._acquire_exc
            yield object()

        return _cm()


class ConnectionFailureClassificationTests(unittest.TestCase):
    def test_enotfound_internal_server_error_is_connection_failure(self):
        exc = asyncpg.exceptions.InternalServerError(
            "(ENOTFOUND) tenant/user postgres.example not found"
        )
        self.assertTrue(db.is_connection_failure(exc))

    def test_unique_violation_is_not_connection_failure(self):
        exc = asyncpg.exceptions.UniqueViolationError("duplicate key")
        self.assertFalse(db.is_connection_failure(exc))

    def test_only_select_is_idempotent(self):
        self.assertTrue(db.is_idempotent_sql("SELECT 1"))
        self.assertTrue(db.is_idempotent_sql("  select id from users"))
        self.assertFalse(db.is_idempotent_sql("INSERT INTO users (username) VALUES ($1)"))
        self.assertFalse(db.is_idempotent_sql("UPDATE users SET username=$1"))
        self.assertFalse(
            db.is_idempotent_sql(
                "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x"
            )
        )


class PoolResilienceUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await db.close_pool()

    async def test_check_db_healthy(self):
        raw = _FakeRawPool(fetchval=1)
        db._pool = db.ResilientPool(raw)
        db._dsn = "postgresql://unused:unused@localhost:5432/unused"  # pragma: allowlist secret
        result = await db.check_db()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"], 1)
        self.assertEqual(raw.fetchval_calls, 1)

    async def test_check_db_failed_after_retry(self):
        boom = asyncpg.exceptions.InternalServerError(
            "(ENOTFOUND) tenant/user postgres.example not found"
        )
        raw1 = _FakeRawPool(fetchval=boom)
        raw2 = _FakeRawPool(fetchval=boom)
        db._pool = db.ResilientPool(raw1)
        db._dsn = "postgresql://unused:unused@localhost:5432/unused"  # pragma: allowlist secret

        async def fake_create(_dsn):
            return raw2

        with patch.object(db, "_create_raw_pool", side_effect=fake_create):
            with self.assertRaises(db.DatabaseUnavailableError):
                await db.check_db()
        self.assertEqual(raw1.fetchval_calls, 1)
        self.assertEqual(raw2.fetchval_calls, 1)
        self.assertTrue(raw1.closed)

    async def test_stale_read_recovers_once(self):
        boom = ConnectionResetError("connection reset by peer")
        raw1 = _FakeRawPool(fetchval=boom)
        raw2 = _FakeRawPool(fetchval=1)
        db._pool = db.ResilientPool(raw1)
        db._dsn = "postgresql://unused:unused@localhost:5432/unused"  # pragma: allowlist secret

        async def fake_create(_dsn):
            return raw2

        with patch.object(db, "_create_raw_pool", side_effect=fake_create):
            value = await db.pool().fetchval("SELECT 1")
        self.assertEqual(value, 1)
        self.assertEqual(raw1.fetchval_calls, 1)
        self.assertEqual(raw2.fetchval_calls, 1)
        self.assertTrue(raw1.closed)

    async def test_write_is_not_retried(self):
        boom = ConnectionResetError("connection reset by peer")
        raw1 = _FakeRawPool(execute=boom)
        raw2 = _FakeRawPool(execute="DELETE 1")
        db._pool = db.ResilientPool(raw1)
        db._dsn = "postgresql://unused:unused@localhost:5432/unused"  # pragma: allowlist secret

        async def fake_create(_dsn):
            return raw2

        with patch.object(db, "_create_raw_pool", side_effect=fake_create):
            with self.assertRaises(db.DatabaseUnavailableError):
                await db.pool().execute("DELETE FROM author_projects WHERE id = $1::uuid", "x")
        self.assertEqual(raw1.execute_calls, 1)
        self.assertEqual(raw2.execute_calls, 0)
        self.assertIs(db._pool._raw, raw2)

    async def test_insert_returning_fetchrow_is_not_retried(self):
        boom = ConnectionResetError("connection reset by peer")
        raw1 = _FakeRawPool(fetchrow=boom)
        raw2 = _FakeRawPool(fetchrow={"id": "new"})
        db._pool = db.ResilientPool(raw1)
        db._dsn = "postgresql://unused:unused@localhost:5432/unused"  # pragma: allowlist secret

        async def fake_create(_dsn):
            return raw2

        with patch.object(db, "_create_raw_pool", side_effect=fake_create):
            with self.assertRaises(db.DatabaseUnavailableError):
                await db.pool().fetchrow(
                    "INSERT INTO author_projects (user_id, title) VALUES ($1, $2) RETURNING id",
                    "u",
                    "t",
                )
        self.assertEqual(raw1.fetchrow_calls, 1)
        self.assertEqual(raw2.fetchrow_calls, 0)

    async def test_structured_failure_logging_includes_pool_stats(self):
        boom = asyncpg.exceptions.InterfaceError("pool is closed")
        raw = _FakeRawPool()
        db._pool = db.ResilientPool(raw)
        token = db.request_id_var.set("req-test-123")
        try:
            with self.assertLogs("lifesight.db", level="ERROR") as captured:
                db.log_db_failure(boom)
        finally:
            db.request_id_var.reset(token)
        joined = "\n".join(captured.output)
        self.assertIn("exception_type=InterfaceError", joined)
        self.assertIn("request_id=req-test-123", joined)
        self.assertIn("pool_size=1", joined)
        self.assertIn("idle_size=1", joined)


class DegradedStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await db.close_pool()

    async def test_init_pool_degraded_when_startup_probe_fails(self):
        boom = asyncpg.exceptions.InternalServerError(
            "(ENOTFOUND) tenant/user postgres.example not found"
        )
        raw = _FakeRawPool(fetchval=boom)

        async def fake_create(_dsn):
            return raw

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@localhost:5432/db"}, clear=False):  # pragma: allowlist secret
            with patch.object(db, "_create_raw_pool", side_effect=fake_create):
                await db.init_pool()
        self.assertIsNone(db._pool)
        with self.assertRaises(db.DatabaseUnavailableError):
            await db.check_db()


class HealthDbRouteTests(unittest.TestCase):
    def setUp(self):
        use_memory_store(True)
        self.addCleanup(lambda: use_memory_store(False))

    def _client(self):
        from main import app

        return TestClient(app)

    def test_health_db_ok(self):
        raw = _FakeRawPool(fetchval=1)

        async def fake_init():
            db._pool = db.ResilientPool(raw)
            db._dsn = "postgresql://unused:unused@localhost:5432/unused"  # pragma: allowlist secret

        with _env():
            with patch("shared.db.init_pool", side_effect=fake_init):
                with patch("shared.db.close_pool", new_callable=AsyncMock):
                    with self._client() as client:
                        resp = client.get("/health/db")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("pool_size", body)
        self.assertIn("X-Request-ID", resp.headers)

    def test_health_db_sanitized_503(self):
        boom = asyncpg.exceptions.InternalServerError(
            "(ENOTFOUND) tenant/user postgres.example not found"
        )
        raw1 = _FakeRawPool(fetchval=boom)
        raw2 = _FakeRawPool(fetchval=boom)

        async def fake_init():
            db._pool = db.ResilientPool(raw1)
            db._dsn = "postgresql://unused:unused@localhost:5432/unused"  # pragma: allowlist secret

        async def fake_create(_dsn):
            return raw2

        with _env():
            with patch("shared.db.init_pool", side_effect=fake_init):
                with patch("shared.db.close_pool", new_callable=AsyncMock):
                    with patch("shared.db._create_raw_pool", side_effect=fake_create):
                        with self._client() as client:
                            resp = client.get(
                                "/health/db",
                                headers={"X-Request-ID": "client-rid-9"},
                            )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"detail": "Database temporarily unavailable"})
        self.assertNotIn("ENOTFOUND", resp.text)
        self.assertNotIn("postgres.example", resp.text)
        self.assertEqual(resp.headers.get("X-Request-ID"), "client-rid-9")


if __name__ == "__main__":
    unittest.main()
