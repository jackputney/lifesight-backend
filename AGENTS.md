# AGENTS.md — LIFESIGHT shared brain

Read by both coding agents on this project: Cursor here in `lifesight-backend`, and
Claude Code in `lifesight-ios`. This file is the source of truth for the contract
between the two repos — if something here doesn't work for how you want to build a
feature, that's a conversation to have before changing it, not a unilateral edit,
because changing it means changing both repos.

## What this project is
LIFESIGHT: life-management software for a visually impaired user. A voice-first iOS
app talks to exactly one backend, which routes by mode to Claude with a mode-specific
system prompt. Three modes today: **author** (manuscript, Google Docs), **health**
(diet/workout logging against an uploaded plan), **jarvis** (calendar + email).

## The frozen API contract
Full detail lives in `.cursor/rules/10-api-contract.mdc` in this repo. Summary:

- `POST /chat {transcript, mode, conversation_id}` → `{reply, mode, conversation_id, pending_action}`
- `POST /confirm {action_id, approved}` → `{result}`
- `GET /me` → `{user_id}`
- `POST /devices`, `GET /devices`, `DELETE /devices/{device_id}` — push-token registration
- `GET /modes` → `{modes: [...]}`
- `GET /health` → `{status: "ok"}`
- `GET /oauth/google/authorize` → `{authorization_url}` (Bearer auth; client opens URL in browser)
- `GET /oauth/google/callback` — Google redirect; signed `state` carries identity; `200` HTML on success
- Auth: `Authorization: Bearer <token>` on every request except `/oauth/google/callback`,
  resolved via `Depends(get_current_user_id)` (`shared/auth.py`). `AUTH_MODE=dev` (default)
  always resolves to a fixed dev UUID; `AUTH_MODE=real` verifies a Supabase JWT. Frozen
  decision: Supabase Auth + Sign in with Apple, no password — see `CONTEXT.md`.

iOS-facing walkthrough of the same contract (including the Google connect sequence):
`MOBILE_API_GUIDE.md`.

## The Confirm Gate — non-negotiable
The user cannot glance at a screen to catch a mistake. Every irreversible action (send
email, create/modify calendar event, write manuscript, save health log) must go through:
draft on `/chat` → `pending_action` returned → spoken confirm in the app → `/confirm`
with `approved: true` → only then does it execute. No exceptions for "obviously safe"
actions. `pending_action.description` is read aloud, so write it as a spoken sentence.

## No real personal data in either repo
Both repos are public. Never commit real names, health numbers, doc IDs, emails, or
tokens — env vars and the database only. Jarvis development uses a separate TEST Google
account, never the real user's calendar/email, until a supervised integration test.

## Mode routing
`main.py`'s `MODE_REGISTRY` maps a `mode` string to a system prompt from
`modes/<mode>/prompt.py`, layered on `shared/identity.py`. Tool sets are pre-built per
mode, never assigned dynamically from user input.

## Where each agent's ownership starts and stops
- Cursor / this repo: everything under `lifesight-backend` except Jarvis's own tool
  implementations once that work starts (owned by whoever is building Jarvis Mode).
- Claude Code / `lifesight-ios`: the SwiftUI app, `LifesightAPI.swift`, Keychain-based
  session storage. The app talks to the backend only — never to Claude or Google
  directly, and never holds an API key on-device.

## Zero placeholders
Every function is fully implemented, or the gap is explicitly documented (e.g. in a
repo's README "not yet here" list) — never a `// TODO` or `# rest of code here` that
looks finished but isn't.
