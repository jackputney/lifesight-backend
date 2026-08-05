"""Run the SQL migrations against DATABASE_URL, in filename order.

Usage (from the repo root, venv active, DATABASE_URL set in .env):

    python scripts/run_migrations.py                  # run migrations/*.sql
    python scripts/run_migrations.py --seed-dev-user  # also insert the AUTH_MODE=dev user

Migrations are mostly idempotent (IF NOT EXISTS / IF EXISTS), but 007's
ALTER TABLE ADD CONSTRAINT is not — re-run only on a DB that has not applied
007 yet, or skip already-applied files manually.

--seed-dev-user inserts the fixed AUTH_MODE=dev UUID into public.users so
domain FKs work under the local bypass. Opt-in; ON CONFLICT DO NOTHING.
"""
import asyncio
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.auth import DEV_FAKE_USER_ID  # noqa: E402

# Unusable Argon2id hash — matches migration 007 stub constant.
_DEV_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$PvirOBKxPzgqJveZrS8AFA$"  # pragma: allowlist secret
    "L1MWnHZ/QAMJI4eOjfP0e/L07+Y5qeYs2k9Mep0rjA0"  # pragma: allowlist secret
)

DEV_SEED_SQL = """
INSERT INTO users (id, username, email, password_hash, display_name, is_active)
VALUES ($1::uuid, 'dev_local', NULL, $2, 'Dev bypass user', TRUE)
ON CONFLICT (id) DO NOTHING;
"""


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    import os
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set — add it to .env first (see README).")

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        for sql_file in sorted((REPO_ROOT / "migrations").glob("*.sql")):
            print(f"Running {sql_file.name} ...")
            await conn.execute(sql_file.read_text(encoding="utf-8"))
            print(f"  ok")

        if "--seed-dev-user" in sys.argv:
            print(f"Seeding dev user {DEV_FAKE_USER_ID} into public.users ...")
            await conn.execute(DEV_SEED_SQL, DEV_FAKE_USER_ID, _DEV_PASSWORD_HASH)
            print("  ok")

        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        print("\nTables now in public schema:")
        for t in tables:
            print(f"  {t['tablename']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
