"""Apply migrations/*.sql in filename order for CI (no .env loading).

Requires DATABASE_URL in the environment. Optionally prepares a stub
auth.users schema so migrations that historically FK Supabase Auth can run
on vanilla Postgres.

Usage:
    DATABASE_URL=postgresql://... python scripts/ci_apply_migrations.py
    DATABASE_URL=postgresql://... python scripts/ci_apply_migrations.py --prepare-auth-stub
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "migrations"
AUTH_STUB = REPO_ROOT / "scripts" / "ci_prepare_auth_schema.sql"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare-auth-stub",
        action="store_true",
        help="Create a minimal auth.users table before applying migrations",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is required (do not rely on .env in CI)")

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        if args.prepare_auth_stub:
            print(f"Preparing auth stub from {AUTH_STUB.name} ...")
            await conn.execute(AUTH_STUB.read_text(encoding="utf-8"))
            print("  ok")

        for sql_file in sorted(MIGRATIONS.glob("*.sql")):
            print(f"Running {sql_file.name} ...")
            await conn.execute(sql_file.read_text(encoding="utf-8"))
            print("  ok")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
