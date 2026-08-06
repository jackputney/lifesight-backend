"""Schema reproducibility — MODE_REGISTRY ↔ migration CHECKs.

Static tests always run (no database).
Live tests run when RUN_MIGRATION_REPRO_TEST=1 and DATABASE_URL point at an
empty disposable Postgres (CI job). Does not require migration 009; when 009
is absent, legacy 'health' may remain in the CHECK until that PR lands.

Run:
    python -m unittest tests.test_migration_repro -v
    RUN_MIGRATION_REPRO_TEST=1 DATABASE_URL=... python -m unittest \\
        tests.test_migration_repro.LiveMigrationReproTests -v
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


def latest_modes_from_migrations(constraint_name: str) -> set[str]:
    """Last committed migration that defines the named CHECK wins."""
    found: set[str] | None = None
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        try:
            found = modes_from_check_sql(text, constraint_name)
        except AssertionError:
            continue
    if found is None:
        raise AssertionError(f"No migration defines {constraint_name}")
    return found


def assert_modes_align_with_registry(allowed: set[str], registry: set[str]) -> None:
    """All MODE_REGISTRY keys must be allowed; extras limited until 009 lands."""
    missing = registry - allowed
    if missing:
        raise AssertionError(
            f"MODE_REGISTRY modes missing from DB CHECK: {sorted(missing)}"
        )
    if MIGRATION_009.is_file():
        if allowed != registry:
            raise AssertionError(
                f"With 009 present, expected exact match; "
                f"db={sorted(allowed)} registry={sorted(registry)}"
            )
    else:
        extras = allowed - registry
        if extras - {"health"}:
            raise AssertionError(
                f"Unexpected modes beyond registry(+legacy health): {sorted(extras)}"
            )


class ModeRegistryMigrationStaticTests(unittest.TestCase):
    def test_committed_mode_checks_cover_mode_registry(self):
        registry = mode_registry_keys()
        self.assertTrue(registry)
        self.assertNotIn("health", registry)
        for name in (
            "conversations_mode_check",
            "action_log_mode_check",
            "pending_actions_source_mode_check",
        ):
            allowed = latest_modes_from_migrations(name)
            assert_modes_align_with_registry(allowed, registry)

    def test_009_matches_mode_registry_when_present(self):
        if not MIGRATION_009.is_file():
            self.skipTest("009_fix_mode_check.sql not in this branch (separate PR)")
        registry = mode_registry_keys()
        sql = MIGRATION_009.read_text(encoding="utf-8")
        for name in (
            "conversations_mode_check",
            "action_log_mode_check",
            "pending_actions_source_mode_check",
        ):
            allowed = modes_from_check_sql(sql, name)
            self.assertEqual(allowed, registry, msg=name)

    def test_migration_filenames_have_no_duplicate_numbers(self):
        numbers: dict[str, list[str]] = {}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            num = path.name.split("_", 1)[0]
            numbers.setdefault(num, []).append(path.name)
        dupes = {k: v for k, v in numbers.items() if len(v) > 1}
        self.assertEqual(dupes, {}, msg=f"duplicate migration numbers: {dupes}")


@unittest.skipUnless(
    os.environ.get("RUN_MIGRATION_REPRO_TEST") == "1",
    "Set RUN_MIGRATION_REPRO_TEST=1 and DATABASE_URL for live migration repro",
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
                    "Use a disposable empty database."
                )

            # Stub auth.users for historical FKs, then apply committed migrations.
            stub = (REPO / "scripts" / "ci_prepare_auth_schema.sql").read_text(
                encoding="utf-8"
            )
            await conn.execute(stub)
            for sql_file in sorted(MIGRATIONS.glob("*.sql")):
                await conn.execute(sql_file.read_text(encoding="utf-8"))

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

            for table, column, cname in MODE_CONSTRAINTS:
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
                assert_modes_align_with_registry(allowed, registry)

            user_id = await conn.fetchval(
                """
                INSERT INTO users (username, password_hash)
                VALUES (
                    'migration_repro',
                    '$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
                )
                RETURNING id
                """  # pragma: allowlist secret
            )

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
        finally:
            await conn.close()


if __name__ == "__main__":
    unittest.main()
