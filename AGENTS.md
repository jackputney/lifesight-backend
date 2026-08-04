# AGENTS.md — LIFESIGHT shared brain (v2)

Read by both coding agents on this project: Cursor here in `lifesight-backend`, and
Claude Code in `lifesight-ios`. This file is the source of truth for the contract
between the two repos — if something here doesn't work for how you want to build a
feature, that's a conversation to have before changing it, not a unilateral edit,
because changing it means changing both repos.

## What this project is
LIFESIGHT: life-management software for a visually impaired user. A voice-first iOS
app talks to exactly one backend, which routes by mode to Claude with a mode-specific
system prompt. **v2 chat modes:** **fitness**, **diet**, **author** (Postgres
chapters/scenes). **jarvis** code remains in the backend repo but is not actively
developed this pass. **settings** is an iOS screen, not a chat mode.

## The API contract (v2)
Full detail lives in `.cursor/rules/10-api-contract.mdc` in this repo. Summary:

- `POST /chat {transcript, mode, conversation_id}` →
  `{reply, mode, conversation_id, pending_action, visual_panel}`
  - `visual_panel` is **optional / nullable** (`{type: string, data: object} | null`).
    Older clients that ignore unknown fields keep working when it is null/absent.
- `POST /confirm {action_id, approved}` → `{result}`
- `GET /me` → `{user_id}`
- `POST /devices`, `GET /devices`, `DELETE /devices/{device_id}` — push-token registration
- `GET /modes` → `{modes: [...]}` — includes `fitness`, `diet`, `author`, and inert `jarvis`
- `GET /health` → `{status: "ok"}`
- Auth (proxied so iOS never holds Supabase keys):
  - `POST /auth/signup` `{email, password}` → session tokens + `user_id`
  - `POST /auth/login` `{email, password}` → session tokens + `user_id`
  - `POST /auth/magic-link` `{email}` → ack (email sent)
  - `POST /auth/apple` `{id_token, nonce?}` → session tokens + `user_id`
  - All other routes: `Authorization: Bearer <token>` via
    `Depends(get_current_user_id)` (`shared/auth.py`). `AUTH_MODE=dev` (default)
    resolves to a fixed dev UUID; `AUTH_MODE=real` verifies a Supabase JWT.
- Domain endpoints (see `routers/v2.py`):
  - Fitness: `POST /workouts/session/start`, `POST /workouts/voice-log`,
    `GET /workouts/session/{id}/state`
  - Diet: `POST /food/photo|barcode|voice` (drafts), `POST /food/entries` (Confirm Gate)
  - Author: `POST /manuscripts`, chapter/scene CRUD, `POST /author/brainstorm`
  - Wearables: `POST /wearables/connect`, `POST /wearables/terra/webhook`

## Resolved product decisions (v2 — do not re-litigate mid-build)
1. Auth: email/password (or magic link) **and** Sign in with Apple.
2. Modes: `fitness` / `diet` / `author`; `jarvis` inert; `health` retired.
3. Author: Postgres manuscripts/chapters/scenes — Google Docs abandoned on this branch.
4. Wearables aggregator: Terra API (default).
5. Contract: additive optional `visual_panel` on chat-shaped responses.
6. Confirm Gate: irreversible/destructive only (not every conversational turn).

## The Confirm Gate — narrowed, still non-negotiable for its scope
The user cannot glance at a screen to catch a mistake. Irreversible/destructive
actions (save food entry, delete scene / overwrite finished chapter, future
export/destructive revision) must go through: draft → `pending_action` → spoken
confirm → `/confirm` with `approved: true` → only then execute.
`pending_action.description` is read aloud — write it as a spoken sentence.

Ordinary workout set logs and reversible scene edits do **not** use the Confirm Gate.

## No real personal data in either repo
Both repos are public. Never commit real names, health numbers, doc IDs, emails, or
tokens — env vars and the database only.

## Mode routing
`main.py`'s `MODE_REGISTRY` maps a `mode` string to a system prompt from
`modes/<mode>/prompt.py`, layered on `shared/identity.py`. Tool sets are pre-built per
mode, never assigned dynamically from user input.

## Where each agent's ownership starts and stops
- Cursor / this repo: everything under `lifesight-backend` except leaving Jarvis
  tools untouched this pass.
- Claude Code / `lifesight-ios`: the SwiftUI app, `LifesightAPI.swift`, Keychain-based
  session storage. The app talks to the backend only — never to Claude, Terra, or
  Supabase Auth directly with embedded secrets.

## Branching
v2 work is on `v2-rebuild`. `main` @ `f3d97158` preserves Author Google Docs WIP.
Do not rewrite `main` history.

## Zero placeholders
Every function is fully implemented, or the gap is explicitly documented (e.g. in a
repo's README "not yet here" list) — never a `// TODO` or `# rest of code here` that
looks finished but isn't.
