"""Schema reproducibility — MODE_REGISTRY ↔ migration CHECKs.

Always-on: static parse of migration 009 vs MODE_REGISTRY in main.py.
Optional live: set RUN_MIGRATION_REPRO_TEST=1 and DATABASE_URL to an empty
throwaway Postgres. Applies a minimal auth.users stub so historical FKs
from 001–003 can run on vanilla Postgres (not used in production).

Run:  python -m unittest tests.test_migration_repro -v
"""

from __future__ import annotations

import ast
import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "migrations"
MIGRATION_009 = MIGRATIONS / "009_fix_mode_check.sql"

AUTH_STUB_SQL = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (id UUID PRIMARY KEY);
"""

EXPECTED_TABLES = (
    "users",
    "auth_sessions",
    "author_projects",
    "author_documents",
    "author_document_versions",
    "conversations",
    "pending_actions",
    "action_log",
)

EXPECTED_FOREIGN_KEYS = (
    ("auth_sessions", "user_id", "users", "id"),
    ("author_projects", "user_id", "users", "id"),
    ("author_documents", "project_id", "author_projects", "id"),
    ("author_documents", "user_id", "users", "id"),
    ("author_document_versions", "document_id", "author_documents", "id"),
    ("author_document_versions", "user_id", "users", "id"),
    ("conversations", "user_id", "users", "id"),
)

MODE_CONSTRAINTS = (
    ("conversations", "mode", "conversations_mode_check"),
    ("action_log", "mode", "action_log_mode_check"),
    ("pending_actions", "source_mode", "pending_actions_source_mode_check"),
)


def mode_registry_keys() -> set[str]:
    """Parse MODE_REGISTRY keys from main.py without importing the app."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "MODE_REGISTRY":
                if not isinstance(node.value, ast.Dict):
                    raise AssertionError("MODE_REGISTRY is not a dict literal")
                keys: set[str] = set()
                for key in node.value.keys:
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        raise AssertionError("MODE_REGISTRY keys must be string literals")
                    keys.add(key.value)
                return keys
    raise AssertionError("MODE_REGISTRY not found in main.py")


def modes_from_check_sql(sql: str, constraint_name: str) -> set[str]:
    """Extract IN (...) string literals for a named CHECK constraint ADD."""
    pattern = re.compile(
        rf"ADD CONSTRAINT {re.escape(constraint_name)}\s+"
        rf"CHECK \((?:mode|source_mode) IN \((.*?)\)\)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(sql)
    if not match:
        raise AssertionError(f"constraint {constraint_name} not found in SQL")
    return set(re.findall(r"'([^']+)'", match.group(1)))


class ModeRegistryMigrationStaticTests(unittest.TestCase):
    def test_009_matches_mode_registry(self):
        registry = mode_registry_keys()
        self.assertEqual(
            registry,
            {
                "fitness",
                "diet",
                "author",
                "brainstorm",
                "mail_calendar",
                "jarvis",
            },
        )
        self.assertTrue(MIGRATION_009.is_file(), "009_fix_mode_check.sql missing")
        sql = MIGRATION_009.read_text(encoding="utf-8")
        for name in (
            "conversations_mode_check",
            "action_log_mode_check",
            "pending_actions_source_mode_check",
        ):
            allowed = modes_from_check_sql(sql, name)
            self.assertEqual(
                allowed,
                registry,
                msg=f"{name}: db={sorted(allowed)} registry={sorted(registry)}",
            )
        self.assertNotIn("health", registry)
        self.assertIn(
            "UPDATE conversations SET mode = 'fitness' WHERE mode = 'health'",
            sql,
        )
        self.assertIn(
            "UPDATE action_log SET mode = 'fitness' WHERE mode = 'health'",
            sql,
        )
        self.assertIn(
            "UPDATE pending_actions SET source_mode = 'fitness' WHERE source_mode = 'health'",
            sql,
        )

    def test_migration_filenames_have_no_duplicate_numbers(self):
        numbers: dict[str, list[str]] = {}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            num = path.name.split("_", 1)[0]
            numbers.setdefault(num, []).append(path.name)
        dupes = {k: v for k, v in numbers.items() if len(v) > 1}
        self.assertEqual(dupes, {}, msg=f"duplicate migration numbers: {dupes}")


@unittest.skipUnless(
    os.environ.get("RUN_MIGRATION_REPRO_TEST") == "1",
    "Set RUN_MIGRATION_REPRO_TEST=1 and DATABASE_URL to run live migration repro",
)
class LiveMigrationReproTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_apply_and_mode_constraint(self):
        import asyncpg

        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            self.skipTest("DATABASE_URL not set")

        registry = mode_registry_keys()
        conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=60)
        try:
            existing = await conn.fetchval(
                """
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                """
            )
            if int(existing) > 0:
                self.fail(
                    "Live migration repro refuses a non-empty public schema. "
                    "Point DATABASE_URL at an empty throwaway database."
                )

            await conn.execute(AUTH_STUB_SQL)

            # Apply 001–008, seed a retired health row, then apply 009.
            for sql_file in sorted(MIGRATIONS.glob("*.sql")):
                if sql_file.name.startswith("009_"):
                    break
                print(f"APPLY {sql_file.name}", flush=True)
                await conn.execute(sql_file.read_text(encoding="utf-8"))

            auth_user = await conn.fetchval(
                "INSERT INTO auth.users (id) VALUES (gen_random_uuid()) RETURNING id"
            )
            # After 007, conversations.user_id references public.users — create one.
            user_id = await conn.fetchval(
                """
                INSERT INTO users (username, password_hash)
                VALUES (
                    'migration_repro_health',
                    '$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
                )
                RETURNING id
                """  # pragma: allowlist secret
            )
            # Prove remap path: insert health while 004 CHECK still allows it.
            health_convo = await conn.fetchval(
                """
                INSERT INTO conversations (id, user_id, mode)
                VALUES (gen_random_uuid(), $1::uuid, 'health')
                RETURNING id
                """,
                user_id,
            )
            # auth_user kept only so the stub insert is not unused if schema differs.
            self.assertIsNotNone(auth_user)

            for sql_file in sorted(MIGRATIONS.glob("009_*.sql")):
                print(f"APPLY {sql_file.name}", flush=True)
                await conn.execute(sql_file.read_text(encoding="utf-8"))

            remapped = await conn.fetchval(
                "SELECT mode FROM conversations WHERE id = $1::uuid",
                health_convo,
            )
            self.assertEqual(remapped, "fitness")

            for table in EXPECTED_TABLES:
                exists = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = $1
                    )
                    """,
                    table,
                )
                self.assertTrue(exists, msg=table)

            for table, column, ref_table, ref_column in EXPECTED_FOREIGN_KEYS:
                row = await conn.fetchrow(
                    """
                    SELECT c.conname
                    FROM pg_constraint c
                    JOIN pg_class cl ON cl.oid = c.conrelid
                    JOIN pg_namespace ns ON ns.oid = cl.relnamespace
                    JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS ck(attnum, ord) ON true
                    JOIN pg_attribute att
                      ON att.attrelid = c.conrelid AND att.attnum = ck.attnum
                    JOIN pg_class ref ON ref.oid = c.confrelid
                    JOIN LATERAL unnest(c.confkey) WITH ORDINALITY AS fk(attnum, ord)
                      ON fk.ord = ck.ord
                    JOIN pg_attribute ref_att
                      ON ref_att.attrelid = c.confrelid AND ref_att.attnum = fk.attnum
                    WHERE c.contype = 'f'
                      AND ns.nspname = 'public'
                      AND cl.relname = $1
                      AND att.attname = $2
                      AND ref.relname = $3
                      AND ref_att.attname = $4
                    """,
                    table,
                    column,
                    ref_table,
                    ref_column,
                )
                self.assertIsNotNone(
                    row,
                    msg=f"missing FK {table}.{column} -> {ref_table}.{ref_column}",
                )

            for _table, _column, cname in MODE_CONSTRAINTS:
                definition = await conn.fetchval(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conname = $1
                    """,
                    cname,
                )
                self.assertIsNotNone(definition, msg=cname)
                allowed = set(re.findall(r"'([^']+)'", definition))
                missing = registry - allowed
                extra = allowed - registry
                self.assertEqual(
                    missing,
                    set(),
                    msg=f"{cname} missing_from_db={sorted(missing)}",
                )
                self.assertEqual(
                    extra,
                    set(),
                    msg=f"{cname} extra_in_db={sorted(extra)}",
                )
                self.assertEqual(allowed, registry, msg=cname)

            for mode in sorted(registry):
                await conn.execute(
                    """
                    INSERT INTO conversations (id, user_id, mode)
                    VALUES (gen_random_uuid(), $1::uuid, $2)
                    """,
                    user_id,
                    mode,
                )

            with self.assertRaises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO conversations (id, user_id, mode)
                    VALUES (gen_random_uuid(), $1::uuid, 'not_a_mode')
                    """,
                    user_id,
                )

            with self.assertRaises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO conversations (id, user_id, mode)
                    VALUES (gen_random_uuid(), $1::uuid, 'health')
                    """,
                    user_id,
                )
        finally:
            await conn.close()


if __name__ == "__main__":
    unittest.main()
