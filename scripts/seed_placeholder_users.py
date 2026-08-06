"""Seed obviously-fake users so the admin panel can be exercised.

    python scripts/seed_placeholder_users.py           # create
    python scripts/seed_placeholder_users.py --clear   # remove them again

Every row is tagged three ways so it can never be mistaken for real data and
is trivially reversible: a fixed UUID prefix (dddddddd-...), an @lifesight.test
email (.test is a reserved TLD that can never resolve), and profiles.status in
('test','suspended'). --clear deletes exactly these UUIDs and nothing else.

Identity: migrations 006/007 moved login identity to self-hosted public.users
(username/password). This inserts there, not Supabase auth.users — the latter
is no longer the identity source under AUTH_MODE=self. Seeded accounts use an
unusable Argon2id hash (same stub constant migration 007 uses) — they exist to
be looked up in the admin panel, not to be logged into.

NOTE: DATABASE_URL currently points at a database that also holds Jack's v2
data, so this is not a private sandbox. Run --clear when you are done looking.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Fixed UUIDs: re-running is idempotent, and --clear knows exactly what to drop.
PREFIX = "dddddddd-0000-4000-8000-0000000000"

# Unusable Argon2id hash — matches migration 007's stub constant. These
# accounts can never authenticate; they exist only to populate the panel.
_UNUSABLE_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$PvirOBKxPzgqJveZrS8AFA$"  # pragma: allowlist secret
    "L1MWnHZ/QAMJI4eOjfP0e/L07+Y5qeYs2k9Mep0rjA0"  # pragma: allowlist secret
)

# Shaped to exercise every state the panel can render: a fully-populated
# primary user, a sparse profile, a suspended account, and (via NO_PROFILE)
# an account with no profile row at all.
PEOPLE = [
    {
        "n": "01", "username": "placeholder_margaret", "email": "placeholder.margaret@lifesight.test",
        "display_name": "Margaret", "full_name": "Margaret Ellison",
        "pronouns": "she/her", "date_of_birth": "1952-06-09",
        "sex_at_birth": "female", "height_cm": 163.0, "weight_kg": 61.5,
        "speech_rate": 0.9, "timezone": "America/Los_Angeles", "locale": "en-US",
        "is_primary": True, "status": "test",
        "notes": "Placeholder for the near-blind primary user. Prefers slower "
                 "speech and full sentence read-back before any send.",
    },
    {
        "n": "02", "username": "placeholder_dev", "email": "placeholder.dev@lifesight.test",
        "display_name": "Dev Tester", "full_name": None, "pronouns": "they/them",
        "date_of_birth": None, "sex_at_birth": "undisclosed",
        "height_cm": None, "weight_kg": None, "speech_rate": 1.0,
        "timezone": "UTC", "locale": "en-US", "is_primary": False,
        "status": "test", "notes": "Sparse profile — no DOB, no body metrics.",
    },
    {
        "n": "03", "username": "placeholder_suspended", "email": "placeholder.suspended@lifesight.test",
        "display_name": "Suspended Sam", "full_name": "Samuel Reyes",
        "pronouns": "he/him", "date_of_birth": "1988-11-30",
        "sex_at_birth": "male", "height_cm": 180.5, "weight_kg": 84.0,
        "speech_rate": 1.25, "timezone": "America/New_York", "locale": "en-US",
        "is_primary": False, "status": "suspended",
        "notes": "Suspended account — checks the status pill renders.",
    },
]
NO_PROFILE = {"n": "04", "username": "placeholder_noprofile", "email": "placeholder.noprofile@lifesight.test"}

ALL_IDS = [PREFIX + p["n"] for p in PEOPLE] + [PREFIX + NO_PROFILE["n"]]

PROFILE_COLS = [
    "display_name", "full_name", "pronouns", "date_of_birth", "sex_at_birth",
    "height_cm", "weight_kg", "speech_rate", "timezone", "locale",
    "is_primary", "status", "notes",
]


async def clear(con: asyncpg.Connection) -> None:
    # profiles/auth_sessions cascade from users, but delete explicitly so the
    # printed count is honest rather than relying on cascade silently working.
    p = await con.fetchval(
        "WITH d AS (DELETE FROM profiles WHERE user_id = ANY($1::uuid[]) RETURNING 1)"
        " SELECT count(*) FROM d", ALL_IDS)
    u = await con.fetchval(
        "WITH d AS (DELETE FROM users WHERE id = ANY($1::uuid[]) RETURNING 1)"
        " SELECT count(*) FROM d", ALL_IDS)
    print(f"removed {u} placeholder users row(s), {p} profile row(s)")


async def seed(con: asyncpg.Connection) -> None:
    # Clear first so re-running is idempotent even if the shape changed.
    await clear(con)

    for p in PEOPLE + [NO_PROFILE]:
        await con.execute(
            """INSERT INTO users (id, username, email, password_hash, display_name, is_active, created_at)
               VALUES ($1::uuid, $2, $3, $4, $5, TRUE, now())
               ON CONFLICT (id) DO NOTHING""",
            PREFIX + p["n"], p["username"], p["email"], _UNUSABLE_PASSWORD_HASH,
            p.get("display_name"))

    placeholders = ", ".join(f"${i + 2}" for i in range(len(PROFILE_COLS)))
    sql = (f"INSERT INTO profiles (user_id, {', '.join(PROFILE_COLS)}) "
           f"VALUES ($1::uuid, {placeholders})")
    for p in PEOPLE:
        vals = []
        for c in PROFILE_COLS:
            v = p[c]
            if c == "date_of_birth" and v:
                from datetime import date
                v = date.fromisoformat(v)
            vals.append(v)
        await con.execute(sql, PREFIX + p["n"], *vals)

    print(f"seeded {len(PEOPLE) + 1} placeholder users "
          f"({len(PEOPLE)} with profiles, 1 without)")
    for r in await con.fetch(
            """SELECT u.username, p.display_name, p.status, p.is_primary,
                      CASE WHEN p.date_of_birth IS NULL THEN NULL
                           ELSE EXTRACT(YEAR FROM age(p.date_of_birth))::INT END AS age
               FROM users u LEFT JOIN profiles p ON p.user_id = u.id
               WHERE u.id = ANY($1::uuid[]) ORDER BY u.username""", ALL_IDS):
        tag = "primary" if r["is_primary"] else (r["status"] or "no profile")
        print(f"  {r['username']:<24} {str(r['display_name'] or '—'):<16} "
              f"age={str(r['age'] or '—'):<4} {tag}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true", help="remove the placeholder rows")
    args = ap.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set — add it to .env first.")

    con = await asyncpg.connect(dsn)
    try:
        await (clear(con) if args.clear else seed(con))
    finally:
        await con.close()


if __name__ == "__main__":
    asyncio.run(main())
