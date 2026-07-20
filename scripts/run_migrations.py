"""Run the SQL migrations against DATABASE_URL, in filename order.

Usage (from the repo root, venv active, DATABASE_URL set in .env):

    python scripts/run_migrations.py                  # run migrations/*.sql
    python scripts/run_migrations.py --seed-dev-user  # also insert the AUTH_MODE=dev user

Migrations are idempotent (CREATE TABLE IF NOT EXISTS throughout), so
re-running after adding a new file is safe.

--seed-dev-user inserts the fixed dev UUID from shared/auth.py into
auth.users so FK inserts work under AUTH_MODE=dev. It is opt-in and
ON CONFLICT DO NOTHING, per the note in 001_users_devices.sql — never
run it against a production project you care about keeping pristine.
"""
import asyncio
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.auth import DEV_FAKE_USER_ID  # noqa: E402

DEV_SEED_SQL = """
INSERT INTO auth.users (id, aud, role, email)
VALUES ($1::uuid, 'authenticated', 'authenticated', 'dev@local.test')
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
            print(f"Seeding dev user {DEV_FAKE_USER_ID} into auth.users ...")
            await conn.execute(DEV_SEED_SQL.replace("$1::uuid", f"'{DEV_FAKE_USER_ID}'::uuid"))
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
