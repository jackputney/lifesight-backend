"""LifeSight admin panel — a local operator view of the database.

Deliberately a SEPARATE ASGI app, not a router on main.py: it never becomes
part of the public API surface, it cannot be reached by shipping the mobile
backend, and it does not touch a file in anyone else's lane.

    python -m admin_panel            # http://127.0.0.1:8001

Three guards, all fail-closed:
  1. ADMIN_API_KEY must be set in .env or the app refuses to start.
  2. Every request must come from loopback.
  3. Every request must carry X-Admin-Key matching ADMIN_API_KEY.

Read-only except for profile fields (migration 010). oauth_credentials is
never selected, never returned, and never decrypted here — encrypted Google
tokens have no business in an operator UI.

Identity: migrations 006/007 moved login identity from Supabase auth.users to
self-hosted public.users (username/password, Argon2id). This queries `users`,
never `auth.users` — the latter is no longer the identity source under
AUTH_MODE=self. password_hash is never selected.
"""
from __future__ import annotations

import hmac
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

import asyncpg  # noqa: E402

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
INDEX_HTML = Path(__file__).resolve().parent / "index.html"

# Only these profile columns may be written. Anything else is rejected before
# it reaches SQL, so the column name can never come from user input.
EDITABLE_FIELDS = {
    "display_name", "full_name", "pronouns", "date_of_birth", "sex_at_birth",
    "height_cm", "weight_kg", "speech_rate", "voice_id", "timezone", "locale",
    "notes", "is_primary", "status",
}

# Per-user activity counts shown on the detail page. Table name is a literal
# from this list, never interpolated from a request. A None column means the
# table has no user_id (messages hangs off conversations; brainstorm_sessions
# is keyed by manuscript) — those are counted globally but not per user.
ACTIVITY_TABLES = [
    ("conversations", "user_id"), ("messages", None), ("pending_actions", "user_id"),
    ("action_log", "user_id"), ("memories", "user_id"), ("reminders", "user_id"),
    ("devices", "user_id"), ("workout_sessions", "user_id"), ("food_entries", "user_id"),
    ("manuscripts", "user_id"), ("brainstorm_sessions", None),
    ("health_metrics", "user_id"), ("wearable_connections", "user_id"),
]

# Per-mode activity. Every statement below is a literal defined here and
# parameterised on $1 = user_id; nothing is ever interpolated from a request.
# `state` records where the mode stands after the v2 rebuild, so the panel does
# not present retired surfaces as if they were live.
MODE_SPECS: dict[str, dict[str, Any]] = {
    "author": {
        "label": "Author", "state": "active",
        "counts": {
            "manuscripts": "SELECT count(*) FROM manuscripts WHERE user_id = $1::uuid",
            "chapters": "SELECT count(*) FROM chapters c JOIN manuscripts m ON m.id = c.manuscript_id WHERE m.user_id = $1::uuid",
            "scenes": "SELECT count(*) FROM scenes s JOIN chapters c ON c.id = s.chapter_id JOIN manuscripts m ON m.id = c.manuscript_id WHERE m.user_id = $1::uuid",
            "words written": "SELECT COALESCE(sum(s.word_count),0) FROM scenes s JOIN chapters c ON c.id = s.chapter_id JOIN manuscripts m ON m.id = c.manuscript_id WHERE m.user_id = $1::uuid",
            "brainstorms": "SELECT count(*) FROM brainstorm_sessions b JOIN manuscripts m ON m.id = b.manuscript_id WHERE m.user_id = $1::uuid",
        },
        "recent": """SELECT created_at AS ts, 'Manuscript: ' || COALESCE(title, 'untitled') AS what
                     FROM manuscripts WHERE user_id = $1::uuid ORDER BY created_at DESC LIMIT 8""",
    },
    "fitness": {
        "label": "Fitness", "state": "active",
        "counts": {
            "plans": "SELECT count(*) FROM workout_plans WHERE user_id = $1::uuid",
            "sessions": "SELECT count(*) FROM workout_sessions WHERE user_id = $1::uuid",
            "sets logged": "SELECT count(*) FROM set_logs sl JOIN workout_sessions ws ON ws.id = sl.session_id WHERE ws.user_id = $1::uuid",
            "personal records": "SELECT count(*) FROM personal_records WHERE user_id = $1::uuid",
        },
        "recent": """SELECT COALESCE(started_at, session_date::timestamptz) AS ts,
                            'Workout ' || session_date::text || ' (' || status || ')' AS what
                     FROM workout_sessions WHERE user_id = $1::uuid ORDER BY 1 DESC LIMIT 8""",
    },
    "diet": {
        "label": "Diet", "state": "active",
        "counts": {
            "food entries": "SELECT count(*) FROM food_entries WHERE user_id = $1::uuid",
            "calories logged": "SELECT COALESCE(sum(calories),0)::int FROM food_entries WHERE user_id = $1::uuid",
            "targets set": "SELECT count(*) FROM daily_nutrition_targets WHERE user_id = $1::uuid",
        },
        "recent": """SELECT logged_at AS ts,
                            COALESCE(matched_food_name, '(unmatched)')
                            || COALESCE(' — ' || calories::text || ' kcal', '') AS what
                     FROM food_entries WHERE user_id = $1::uuid ORDER BY logged_at DESC LIMIT 8""",
    },
    "brainstorm": {
        "label": "Brainstorm", "state": "active",
        "counts": {
            "sessions": "SELECT count(*) FROM brainstorm_sessions b JOIN manuscripts m ON m.id = b.manuscript_id WHERE m.user_id = $1::uuid",
        },
        "recent": """SELECT b.created_at AS ts, 'Brainstorm on: ' || COALESCE(m.title,'untitled') AS what
                     FROM brainstorm_sessions b JOIN manuscripts m ON m.id = b.manuscript_id
                     WHERE m.user_id = $1::uuid ORDER BY b.created_at DESC LIMIT 8""",
    },
    "mail_calendar": {
        "label": "Mail & Calendar", "state": "shell — no tools wired yet",
        "counts": {},
        "recent": None,
    },
    "jarvis": {
        "label": "Jarvis", "state": "legacy — replaced by Mail & Calendar",
        "counts": {
            "reminders": "SELECT count(*) FROM reminders WHERE user_id = $1::uuid",
            "memories": "SELECT count(*) FROM memories WHERE user_id = $1::uuid",
        },
        "recent": """SELECT ts, what FROM (
                       SELECT created_at AS ts, 'Reminder: ' || description AS what
                         FROM reminders WHERE user_id = $1::uuid
                       UNION ALL
                       SELECT created_at, 'Memory: ' || left(content, 60)
                         FROM memories WHERE user_id = $1::uuid
                     ) x ORDER BY ts DESC LIMIT 8""",
    },
    "health": {
        "label": "Health", "state": "retired — superseded by Fitness + Diet",
        "counts": {
            "plans": "SELECT count(*) FROM health_plans WHERE user_id = $1::uuid",
            "entries": "SELECT count(*) FROM health_entries WHERE user_id = $1::uuid",
            "metrics": "SELECT count(*) FROM health_metrics WHERE user_id = $1::uuid",
            "wearables": "SELECT count(*) FROM wearable_connections WHERE user_id = $1::uuid",
        },
        "recent": """SELECT recorded_at AS ts,
                            entry_type || COALESCE(': ' || value_text,
                                          COALESCE(': ' || value_numeric::text, '')) AS what
                     FROM health_entries WHERE user_id = $1::uuid ORDER BY recorded_at DESC LIMIT 8""",
    },
}

_pool: Optional[asyncpg.Pool] = None


def _admin_key() -> str:
    return os.environ.get("ADMIN_API_KEY", "").strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set — see .env.example.")
    if not _admin_key():
        raise RuntimeError(
            "ADMIN_API_KEY is not set. Generate one and add it to .env:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        yield
    finally:
        if _pool is not None:
            await _pool.close()
            _pool = None


app = FastAPI(title="LifeSight Admin", lifespan=lifespan, docs_url=None, redoc_url=None)


async def guard(request: Request, x_admin_key: str = Header(default="")) -> None:
    """Loopback + constant-time key check. Fails closed."""
    host = (request.client.host if request.client else "") or ""
    if host not in LOOPBACK:
        raise HTTPException(status_code=403, detail="Admin panel is loopback-only.")
    expected = _admin_key()
    if not expected or not hmac.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=401, detail="Bad or missing X-Admin-Key.")


class ProfilePatch(BaseModel):
    """All optional — only the keys actually sent are written."""
    display_name: Optional[str] = None
    full_name: Optional[str] = None
    pronouns: Optional[str] = None
    # A real date, not a string: the column is DATE and asyncpg will not
    # coerce "1968-03-14" for it. Pydantic parses the ISO form for us.
    date_of_birth: Optional[date] = None
    sex_at_birth: Optional[str] = None
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    speech_rate: Optional[float] = Field(default=None, ge=0.5, le=2.0)
    voice_id: Optional[str] = None
    timezone: Optional[str] = None
    locale: Optional[str] = None
    notes: Optional[str] = None
    is_primary: Optional[bool] = None
    status: Optional[str] = None


def _pool_or_die() -> asyncpg.Pool:
    if _pool is None:
        raise HTTPException(status_code=503, detail="Database pool is not ready.")
    return _pool


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    # The page itself is not secret; every data call below still needs the key.
    return FileResponse(INDEX_HTML)


@app.get("/api/stats", dependencies=[Depends(guard)])
async def stats() -> dict[str, Any]:
    async with _pool_or_die().acquire() as con:
        users = await con.fetchval("SELECT count(*) FROM users")
        active_users = await con.fetchval("SELECT count(*) FROM users WHERE is_active")
        profiles = await con.fetchval("SELECT count(*) FROM profiles")
        counts = {}
        for table, _ in ACTIVITY_TABLES:
            try:
                counts[table] = await con.fetchval(f"SELECT count(*) FROM {table}")
            except asyncpg.PostgresError:
                counts[table] = None  # table not in this database yet
        modes = await con.fetch(
            "SELECT mode, count(*) c FROM conversations GROUP BY 1 ORDER BY 2 DESC"
        )
    return {
        "users": users,
        "active_users": active_users,
        "profiles": profiles,
        "missing_profiles": (users or 0) - (profiles or 0),
        "tables": counts,
        "conversations_by_mode": [{"mode": r["mode"], "count": r["c"]} for r in modes],
    }


@app.get("/api/users", dependencies=[Depends(guard)])
async def list_users() -> list[dict[str, Any]]:
    async with _pool_or_die().acquire() as con:
        rows = await con.fetch(
            """
            SELECT u.id, u.username, u.email, u.is_active, u.created_at,
                   -- last_used_at across sessions is the self-hosted-auth
                   -- equivalent of Supabase's last_sign_in_at.
                   (SELECT max(s.last_used_at) FROM auth_sessions s WHERE s.user_id = u.id) AS last_active_at,
                   COALESCE(p.display_name, u.display_name) AS display_name,
                   p.status, p.is_primary, p.date_of_birth,
                   CASE WHEN p.date_of_birth IS NULL THEN NULL
                        ELSE EXTRACT(YEAR FROM age(p.date_of_birth))::INT END AS age_years,
                   p.user_id IS NOT NULL AS has_profile,
                   (SELECT count(*) FROM conversations c WHERE c.user_id = u.id) AS conversations
            FROM users u
            LEFT JOIN profiles p ON p.user_id = u.id
            ORDER BY p.is_primary DESC NULLS LAST, u.created_at
            """
        )
    return [dict(r) for r in rows]


@app.get("/api/users/{user_id}", dependencies=[Depends(guard)])
async def user_detail(user_id: UUID) -> dict[str, Any]:
    # UUID type means FastAPI rejects a malformed id with 422 before any query.
    user_id = str(user_id)
    async with _pool_or_die().acquire() as con:
        user = await con.fetchrow(
            """SELECT id, username, email, display_name, is_active, created_at, updated_at
               FROM users WHERE id = $1::uuid""",
            user_id,
        )
        if user is None:
            raise HTTPException(status_code=404, detail="No such user.")
        profile = await con.fetchrow("SELECT * FROM profiles WHERE user_id = $1::uuid", user_id)
        sessions = await con.fetchrow(
            """SELECT count(*) AS active_sessions, max(last_used_at) AS last_active_at
               FROM auth_sessions WHERE user_id = $1::uuid AND revoked_at IS NULL
                 AND expires_at > now()""",
            user_id,
        )

        activity: dict[str, Any] = {}
        for table, col in ACTIVITY_TABLES:
            if col is None:
                continue  # messages joins through conversations; counted below
            try:
                activity[table] = await con.fetchval(
                    f"SELECT count(*) FROM {table} WHERE {col} = $1::uuid", user_id
                )
            except asyncpg.PostgresError:
                activity[table] = None
        try:
            activity["messages"] = await con.fetchval(
                """SELECT count(*) FROM messages m
                   JOIN conversations c ON c.id = m.conversation_id
                   WHERE c.user_id = $1::uuid""",
                user_id,
            )
        except asyncpg.PostgresError:
            activity["messages"] = None

        convos = await con.fetch(
            """SELECT id AS conversation_id, mode, title, started_at, last_message_at
               FROM conversations WHERE user_id = $1::uuid
               ORDER BY COALESCE(last_message_at, started_at) DESC LIMIT 10""",
            user_id,
        )
        # Presence only — the panel never renders token material.
        google = await con.fetchval(
            "SELECT count(*) FROM oauth_credentials WHERE user_id = $1::uuid", user_id
        )

    return {
        "user": dict(user),
        "sessions": dict(sessions) if sessions else {"active_sessions": 0, "last_active_at": None},
        "profile": dict(profile) if profile else None,
        "activity": activity,
        "recent_conversations": [dict(r) for r in convos],
        "google_connected": bool(google),
    }


@app.get("/api/users/{user_id}/activity", dependencies=[Depends(guard)])
async def user_activity(user_id: UUID) -> dict[str, Any]:
    """What the user has actually DONE, grouped by mode.

    Separate from /api/users/{id} so the detail pane stays fast: this fans out
    across every domain table and is only fetched when the operator asks.
    """
    user_id = str(user_id)
    async with _pool_or_die().acquire() as con:
        # The Confirm Gate trail — the truest record of user intent and outcome.
        actions = await con.fetch(
            """SELECT id, source_mode, action_type, description, status,
                      confirmed_via, created_at, resolved_at, expires_at
               FROM pending_actions WHERE user_id = $1::uuid
               ORDER BY created_at DESC LIMIT 100""",
            user_id,
        )
        by_status = await con.fetch(
            """SELECT status, count(*) c FROM pending_actions
               WHERE user_id = $1::uuid GROUP BY 1 ORDER BY 2 DESC""",
            user_id,
        )
        # Executed tool calls. Empty today, but this is where /chat will log.
        tools = await con.fetch(
            """SELECT mode, tool_name, result_summary, confirmed, created_at
               FROM action_log WHERE user_id = $1::uuid
               ORDER BY created_at DESC LIMIT 50""",
            user_id,
        )

        modes: dict[str, Any] = {}
        for mode, spec in MODE_SPECS.items():
            counts: dict[str, Any] = {}
            for label, sql in spec["counts"].items():
                try:
                    counts[label] = await con.fetchval(sql, user_id)
                except asyncpg.PostgresError:
                    counts[label] = None  # table absent in this database
            recent: list[dict[str, Any]] = []
            if spec["recent"]:
                try:
                    recent = [dict(r) for r in await con.fetch(spec["recent"], user_id)]
                except asyncpg.PostgresError:
                    recent = []
            convos = await con.fetchval(
                "SELECT count(*) FROM conversations WHERE user_id = $1::uuid AND mode = $2",
                user_id, mode,
            )
            mode_actions = [dict(a) for a in actions if a["source_mode"] == mode]
            modes[mode] = {
                "label": spec["label"],
                "state": spec["state"],
                "counts": counts,
                "recent": recent,
                "conversations": convos,
                "actions": mode_actions,
                # Drives whether the UI collapses the section by default.
                "any": bool(convos) or bool(mode_actions) or any(counts.values())
                       or bool(recent),
            }

    return {
        "actions_by_status": [{"status": r["status"], "count": r["c"]} for r in by_status],
        "actions": [dict(a) for a in actions],
        "tool_calls": [dict(t) for t in tools],
        "modes": modes,
    }


@app.patch("/api/users/{user_id}/profile", dependencies=[Depends(guard)])
async def upsert_profile(user_id: UUID, patch: ProfilePatch) -> dict[str, Any]:
    user_id = str(user_id)
    fields = {k: v for k, v in patch.model_dump(exclude_unset=True).items()
              if k in EDITABLE_FIELDS}
    if not fields:
        raise HTTPException(status_code=400, detail="No editable fields supplied.")

    cols = list(fields)
    placeholders = ", ".join(f"${i + 2}" for i in range(len(cols)))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    sql = (
        f"INSERT INTO profiles (user_id, {', '.join(cols)}) "
        f"VALUES ($1::uuid, {placeholders}) "
        f"ON CONFLICT (user_id) DO UPDATE SET {updates} "
        f"RETURNING *"
    )
    async with _pool_or_die().acquire() as con:
        exists = await con.fetchval("SELECT 1 FROM users WHERE id = $1::uuid", user_id)
        if not exists:
            raise HTTPException(status_code=404, detail="No such user.")
        try:
            row = await con.fetchrow(sql, user_id, *[fields[c] for c in cols])
        except asyncpg.exceptions.CheckViolationError as exc:
            raise HTTPException(status_code=422, detail=f"Value rejected: {exc}") from exc
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise HTTPException(
                status_code=409, detail="Another user is already the primary user."
            ) from exc
        except asyncpg.exceptions.DataError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return dict(row)


def main() -> None:
    import uvicorn

    if not _admin_key():
        key = secrets.token_urlsafe(32)
        raise SystemExit(
            "ADMIN_API_KEY is not set. Add this line to .env and re-run:\n\n"
            f"  ADMIN_API_KEY={key}\n"
        )
    print("Admin panel  ->  http://127.0.0.1:8001")
    print(f"X-Admin-Key  ->  {_admin_key()}")
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")


if __name__ == "__main__":
    main()
