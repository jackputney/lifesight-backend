# Lifesight Backend — Auth, Devices, Conversation Memory & Confirm Gate

Every route now resolves identity via `Depends(get_current_user_id)`
(`shared/auth.py`) instead of trusting a client-supplied `user_id`. Auth is
stubbed: `AUTH_MODE=dev` (the default) resolves every request to a fixed dev
UUID; `AUTH_MODE=real` verifies a Supabase-issued JWT. Swapping modes touches
only `shared/auth.py` — see that file and `CONTEXT.md` for the frozen auth
decision (Supabase + Sign in with Apple).

On top of that: `/chat` takes a `conversation_id` and keeps multi-turn history
per conversation (in-memory, `CONVERSATIONS` in `main.py`, scoped to the
requesting user). Response shape matches the frozen API contract shared with
the iOS app (`.cursor/rules/10-api-contract.mdc`, `AGENTS.md`): `reply`,
`conversation_id`, and a `pending_action` slot. `POST /confirm` resolves a
pending action by id — the Confirm Gate's second half. No mode populates
`pending_action` yet (no tool-calling is wired up), so it's always `null`
today; the shapes exist and work; `main.py`'s in-memory `PENDING_ACTIONS`
mirrors the `pending_actions` table shape (status, expiry, payload) drafted
in `migrations/002_core_schema.sql`, so wiring it to Postgres later won't
change route signatures.

`/me` returns resolved identity. `/devices` (register/list/delete) upserts
push-notification targets per user — in-memory stub backed by
`migrations/001_users_devices.sql`, same swap-later pattern as conversations.

See `CONTEXT.md` for the full set of frozen architecture decisions (auth,
Confirm Gate, sync policy for health logs vs. the manuscript).

## Setup (run once)

1. Make sure you have Python 3.11+ installed.
2. In this folder, create a virtual environment and install dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate          # Mac/Linux
   pip install -r requirements.txt
   ```

   For contributing (secret-scan pre-commit hook, etc.), install dev deps instead:

   ```bash
   pip install -r requirements-dev.txt
   pre-commit install
   ```

3. Copy the example env file and add your real API key:

   ```bash
   cp .env.example .env
   ```

   Then open `.env` and replace the placeholder with your real Anthropic
   API key (starts with `sk-ant-...`). Get one from [console.anthropic.com](https://console.anthropic.com)
   if you don't have one yet. Leave `AUTH_MODE` unset (defaults to `dev`) unless
   you're specifically testing real Supabase JWT verification.

   Also set `DATABASE_URL` — the server won't start without it. Get it from
   the Supabase dashboard: **Connect** (top of the project page) → **Connection
   string** → copy the **Session pooler** URI (the direct connection is
   IPv6-only, which most home networks can't reach) and swap in your database
   password.

3b. Create the database tables (run once per Supabase project, and again any
   time a new migration file lands):

   ```bash
   python scripts/run_migrations.py --seed-dev-user
   ```

   `--seed-dev-user` inserts the fixed `AUTH_MODE=dev` UUID into `auth.users`
   so foreign keys resolve during local dev. Skip that flag on a production
   project — there, real rows come from Sign in with Apple.

4. Load the `.env` file before running (FastAPI doesn't do this
   automatically) — the easiest way is to export it in your shell:

   ```bash
   export $(cat .env | xargs)
   ```

   (On Windows/PowerShell this step is different — ask if you hit this.)

## Run it

```bash
uvicorn main:app --reload
```

You should see:

```
Uvicorn running on http://127.0.0.1:8000
```

## Test it

In a second terminal window, with the server still running:

```bash
curl -s http://localhost:8000/me | python3 -m json.tool
```

In dev mode (default), every request resolves to the same fixed dev UUID —
this confirms the auth plumbing works end to end without a real token.

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "Can you give me a quick recap of what you do?", "mode": "author"}'
```

You'll get back JSON like:

```json
{
  "reply": "...",
  "mode": "author",
  "conversation_id": "3f9c...",
  "pending_action": null
}
```

Copy the `conversation_id` from the response and send a follow-up in the
same conversation to confirm memory works — the reply should make sense as
a continuation, not a fresh answer:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "Can you repeat the last thing you said, word for word?", "mode": "author", "conversation_id": "PASTE_THE_ID_HERE"}'
```

Leaving `conversation_id` out (or `null`) starts a fresh conversation with
a new id every time.

Try switching modes to confirm routing still works:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "What do you help me with here?", "mode": "health"}'

curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "What do you help me with here?", "mode": "jarvis"}'
```

Try an invalid mode to confirm error handling:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"transcript": "hi", "mode": "nonsense"}'
```

Should return a 400 error listing valid modes.

Try `/confirm` with a made-up action id to confirm 404 handling:

```bash
curl -X POST http://localhost:8000/confirm \
  -H "Content-Type: application/json" \
  -d '{"action_id": "does-not-exist", "approved": true}'
```

Should return a 404 — there's nothing to confirm yet since no mode creates
pending actions.

Try registering and listing a device:

```bash
curl -s -X POST http://localhost:8000/devices \
  -H "Content-Type: application/json" \
  -d '{"device_id": "test-device-1", "platform": "ios"}' | python3 -m json.tool

curl -s http://localhost:8000/devices | python3 -m json.tool
```

Also try:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/modes
```

Should return: `{"status":"ok"}` and `{"modes":["author","health","jarvis"]}`

## Project layout

```
main.py                    # /chat, /confirm, /me, /devices, MODE_REGISTRY
shared/auth.py              # get_current_user_id (AUTH_MODE=dev|real)
shared/db.py                # asyncpg pool + every SQL query (routes never touch SQL)
shared/identity.py          # Olivia shared preamble
scripts/run_migrations.py   # applies migrations/*.sql to DATABASE_URL
modes/
  author/prompt.py          # check / write / read-back
  health/prompt.py          # logs against plan
  jarvis/prompt.py          # Oliver's area, confirm-gate rules
migrations/
  001_users_devices.sql     # users + devices (drafted, not yet run against a DB)
  002_core_schema.sql        # conversations, pending_actions, health, writing, etc.
docs/
  health-plan-reference.txt
CONTEXT.md                  # frozen architecture decisions (auth, Confirm Gate, sync)
AGENTS.md                   # cross-repo contract, shared with lifesight-ios
.cursor/rules/               # Cursor guardrails (architecture, contract, style)
.pre-commit-config.yaml      # detect-secrets hook
requirements.txt
requirements-dev.txt         # + pre-commit, detect-secrets
```

## What's NOT here yet (by design)

- No Google Docs reading/writing for Author Mode
- No tool-calling in any mode, so `pending_action` is always `null` — the
  Confirm Gate's shapes exist end-to-end (including its `pending_actions`
  table), but nothing creates rows yet
- No Calendar/Gmail for Jarvis Mode (Oliver's area)
- `AUTH_MODE=real` (actual Supabase JWT verification) is implemented in
  `shared/auth.py` but untested against a live Supabase project; no iOS Sign
  in with Apple flow yet either
- No auth/security beyond the Bearer-token check itself (fine for local
  testing only — rate limiting, HTTPS, etc. come before deploying anywhere
  public)

## If something goes wrong

- **"Could not resolve authentication method"** → your `.env` isn't loaded,
  or the API key is wrong/missing. Re-check step 3–4 above.
- **"unexpected keyword argument 'proxies'"** → a dependency version
  mismatch between `anthropic` and `httpx`. This `requirements.txt` already
  pins the working versions (`httpx==0.28.1`) — if you see this error,
  make sure you actually reinstalled after pulling the latest
  `requirements.txt` (`pip install -r requirements.txt` again).
- **Port 8000 already in use** → another process is using it; either stop
  it or run `uvicorn main:app --reload --port 8001` instead.
- **401 "Missing bearer token"** → you set `AUTH_MODE=real` without sending
  an `Authorization: Bearer <token>` header, or without `SUPABASE_JWT_SECRET`
  configured. Unset `AUTH_MODE` (or set it to `dev`) for local testing.
