"""Schema reproducibility — MODE_REGISTRY ↔ migration CHECKs.

Always-on: migration filename hygiene + static 009↔MODE_REGISTRY exact match.
When RUN_MIGRATION_REPRO_TEST=1: apply committed migrations on empty disposable
Postgres (CI job) with an auth.users stub for historical FKs, then verify
schema/FKs, exact MODE_REGISTRY CHECKs, registry-mode acceptance, and
rejection of health + unknown modes.

Run:
    python -m unittest tests.test_migration_repro -v
    RUN_MIGRATION_REPRO_TEST=1 DATABASE_URL=... python -m unittest \\
        tests.test_migration_repro -v
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
    "user_profiles",
    "admin_audit_log",
    "conversation_turn_metrics",
    "daily_checkins",
    "user_prompt_overrides",
    "google_connections",
    "google_oauth_transactions",
    "health_samples",
    "health_sync_state",
    "author_sessions",
    "author_captures",
    "author_draft_versions",
    "author_flags",
    "author_flag_decisions",
    "personalization_summaries",
    "prompt_change_proposals",
)

# Looked up by (table, column, ref_table, ref_column); name is diagnostic only.
EXPECTED_FOREIGN_KEYS = (
    ("auth_sessions_user_id_fkey", "auth_sessions", "user_id", "users", "id"),
    ("author_projects_user_id_fkey", "author_projects", "user_id", "users", "id"),
    ("author_documents_project_id_fkey", "author_documents", "project_id", "author_projects", "id"),
    ("author_documents_user_id_fkey", "author_documents", "user_id", "users", "id"),
    (
        "author_document_versions_document_id_fkey",
        "author_document_versions",
        "document_id",
        "author_documents",
        "id",
    ),
    (
        "author_document_versions_user_id_fkey",
        "author_document_versions",
        "user_id",
        "users",
        "id",
    ),
    ("conversations_user_id_fkey", "conversations", "user_id", "users", "id"),
    ("user_profiles_user_id_fkey", "user_profiles", "user_id", "users", "id"),
    (
        "conversation_turn_metrics_conversation_id_fkey",
        "conversation_turn_metrics",
        "conversation_id",
        "conversations",
        "id",
    ),
    ("daily_checkins_user_id_fkey", "daily_checkins", "user_id", "users", "id"),
    (
        "user_prompt_overrides_user_id_fkey",
        "user_prompt_overrides",
        "user_id",
        "users",
        "id",
    ),
    (
        "google_connections_user_id_fkey",
        "google_connections",
        "user_id",
        "users",
        "id",
    ),
    (
        "google_oauth_transactions_user_id_fkey",
        "google_oauth_transactions",
        "user_id",
        "users",
        "id",
    ),
    ("health_samples_user_id_fkey", "health_samples", "user_id", "users", "id"),
    ("health_sync_state_user_id_fkey", "health_sync_state", "user_id", "users", "id"),
    ("author_sessions_user_id_fkey", "author_sessions", "user_id", "users", "id"),
    (
        "author_captures_session_id_fkey",
        "author_captures",
        "session_id",
        "author_sessions",
        "id",
    ),
    ("author_captures_user_id_fkey", "author_captures", "user_id", "users", "id"),
    (
        "author_draft_versions_session_id_fkey",
        "author_draft_versions",
        "session_id",
        "author_sessions",
        "id",
    ),
    (
        "author_draft_versions_user_id_fkey",
        "author_draft_versions",
        "user_id",
        "users",
        "id",
    ),
    (
        "author_draft_versions_derived_from_version_id_fkey",
        "author_draft_versions",
        "derived_from_version_id",
        "author_draft_versions",
        "id",
    ),
    ("author_flags_session_id_fkey", "author_flags", "session_id", "author_sessions", "id"),
    (
        "author_flags_draft_version_id_fkey",
        "author_flags",
        "draft_version_id",
        "author_draft_versions",
        "id",
    ),
    ("author_flags_user_id_fkey", "author_flags", "user_id", "users", "id"),
    (
        "author_flag_decisions_flag_id_fkey",
        "author_flag_decisions",
        "flag_id",
        "author_flags",
        "id",
    ),
    (
        "author_flag_decisions_user_id_fkey",
        "author_flag_decisions",
        "user_id",
        "users",
        "id",
    ),
    (
        "author_flag_decisions_resulting_draft_version_id_fkey",
        "author_flag_decisions",
        "resulting_draft_version_id",
        "author_draft_versions",
        "id",
    ),
    (
        "personalization_summaries_user_id_fkey",
        "personalization_summaries",
        "user_id",
        "users",
        "id",
    ),
    (
        "prompt_change_proposals_user_id_fkey",
        "prompt_change_proposals",
        "user_id",
        "users",
        "id",
    ),
    ("workout_days_user_id_fkey", "workout_days", "user_id", "users", "id"),
    ("planned_exercises_user_id_fkey", "planned_exercises", "user_id", "users", "id"),
    ("set_logs_user_id_fkey", "set_logs", "user_id", "users", "id"),
)

MODE_CONSTRAINTS = (
    ("conversations", "mode", "conversations_mode_check"),
    ("action_log", "mode", "action_log_mode_check"),
    ("pending_actions", "source_mode", "pending_actions_source_mode_check"),
    # Nullable allowlists (NULL = global/unscoped). A stale allowlist here
    # silently blocks a newly registered mode: admin cannot apply an override
    # for it, and personalization cannot file a proposal against it.
    ("user_prompt_overrides", "mode", "user_prompt_overrides_mode_chk"),
    ("prompt_change_proposals", "mode", "prompt_change_proposals_mode_chk"),
)

# Registry frozen at migration 009 (pre-checkin).
EXPECTED_REGISTRY_009 = {
    "fitness",
    "diet",
    "author",
    "brainstorm",
    "mail_calendar",
    "jarvis",
}

EXPECTED_REGISTRY = {
    *EXPECTED_REGISTRY_009,
    "checkin",
}


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
    """Extract IN (...) string literals for a named mode CHECK constraint.

    Handles both shapes in use: the `ALTER TABLE ... ADD CONSTRAINT` form and
    the inline `CONSTRAINT ... CHECK (mode IS NULL OR mode IN (...))` form
    used by the nullable allowlists in migrations 014 and 018.
    """
    pattern = re.compile(
        rf"(?:ADD )?CONSTRAINT {re.escape(constraint_name)}\s+CHECK\s*\(\s*"
        rf"(?:(?:mode|source_mode)\s+IS\s+NULL\s+OR\s+)?"
        rf"(?:mode|source_mode)\s+IN\s*\((.*?)\)",
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


def assert_modes_match_registry(allowed: set[str], registry: set[str], *, where: str) -> None:
    """CHECK constraint modes must equal MODE_REGISTRY exactly."""
    missing = registry - allowed
    extra = allowed - registry
    if missing or extra or allowed != registry:
        raise AssertionError(
            f"{where}: mode CHECK does not match MODE_REGISTRY.\n"
            f"  db_allowed={sorted(allowed)}\n"
            f"  registry={sorted(registry)}\n"
            f"  missing_from_db={sorted(missing)}\n"
            f"  extra_in_db={sorted(extra)}"
        )


class MigrationHygieneStaticTests(unittest.TestCase):
    def test_migration_filenames_have_no_duplicate_numbers(self):
        numbers: dict[str, list[str]] = {}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            num = path.name.split("_", 1)[0]
            numbers.setdefault(num, []).append(path.name)
        dupes = {k: v for k, v in numbers.items() if len(v) > 1}
        self.assertEqual(dupes, {}, msg=f"duplicate migration numbers: {dupes}")


class ModeRegistryMigrationStaticTests(unittest.TestCase):
    def test_mode_registry_keys_are_exact(self):
        self.assertEqual(mode_registry_keys(), EXPECTED_REGISTRY)
        self.assertNotIn("health", EXPECTED_REGISTRY)

    def test_committed_mode_checks_match_mode_registry(self):
        registry = mode_registry_keys()
        for name in (
            "conversations_mode_check",
            "action_log_mode_check",
            "pending_actions_source_mode_check",
        ):
            allowed = latest_modes_from_migrations(name)
            assert_modes_match_registry(allowed, registry, where=f"migration SQL {name}")

    def test_009_matches_historical_registry(self):
        self.assertTrue(MIGRATION_009.is_file(), "009_fix_mode_check.sql missing")
        sql = MIGRATION_009.read_text(encoding="utf-8")
        for name in (
            "conversations_mode_check",
            "action_log_mode_check",
            "pending_actions_source_mode_check",
        ):
            allowed = modes_from_check_sql(sql, name)
            assert_modes_match_registry(
                allowed, EXPECTED_REGISTRY_009, where=f"009 SQL {name}"
            )
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


@unittest.skipUnless(
    os.environ.get("RUN_MIGRATION_REPRO_TEST") == "1",
    "Set RUN_MIGRATION_REPRO_TEST=1 and DATABASE_URL for live migration repro",
)
class LiveMigrationReproTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_apply_schema_fks_and_mode_constraint(self):
        import asyncpg

        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            self.skipTest("DATABASE_URL not set")

        registry = mode_registry_keys()
        self.assertEqual(registry, EXPECTED_REGISTRY)

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

            stub = (REPO / "scripts" / "ci_prepare_auth_schema.sql").read_text(
                encoding="utf-8"
            )
            await conn.execute(stub)

            # Apply 001–008, seed a retired health row, then apply 009+.
            for sql_file in sorted(MIGRATIONS.glob("*.sql")):
                if sql_file.name.startswith("009_"):
                    break
                print(f"APPLY {sql_file.name}", flush=True)
                await conn.execute(sql_file.read_text(encoding="utf-8"))

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
            health_convo = await conn.fetchval(
                """
                INSERT INTO conversations (id, user_id, mode)
                VALUES (gen_random_uuid(), $1::uuid, 'health')
                RETURNING id
                """,
                user_id,
            )

            for sql_file in sorted(MIGRATIONS.glob("009_*.sql")):
                print(f"APPLY {sql_file.name}", flush=True)
                await conn.execute(sql_file.read_text(encoding="utf-8"))

            remapped = await conn.fetchval(
                "SELECT mode FROM conversations WHERE id = $1::uuid",
                health_convo,
            )
            self.assertEqual(remapped, "fitness")

            # Apply post-009 migrations in numeric order (010, 011, …).
            for sql_file in sorted(MIGRATIONS.glob("*.sql")):
                prefix = sql_file.name.split("_", 1)[0]
                if not prefix.isdigit() or int(prefix) <= 9:
                    continue
                print(f"APPLY {sql_file.name}", flush=True)
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
                self.assertTrue(exists, msg=f"missing table: {table}")

            for cname, table, column, ref_table, ref_column in EXPECTED_FOREIGN_KEYS:
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
                    msg=(
                        f"missing FK {table}.{column} -> {ref_table}.{ref_column} "
                        f"(expected name ~{cname})"
                    ),
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
                print(f"CONSTRAINT {cname}: {definition}", flush=True)
                allowed = set(re.findall(r"'([^']+)'", definition))
                assert_modes_match_registry(
                    allowed, registry, where=f"live DB {cname}"
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
