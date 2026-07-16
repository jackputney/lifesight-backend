# LifeSite — Architecture & Decisions

Voice-first assistant for a near-blind primary user. Three modes share one
FastAPI backend and one "Olivia" voice identity: **author** (manuscript in
Google Docs), **health** (log against a plan), **jarvis** (calendar/email).
Accessibility (VoiceOver, spoken confirmation) is the dominating constraint
everywhere, not just in Jarvis.

## Confirmed decisions

**Auth — Supabase + Sign in with Apple.** Supabase Auth owns identity;
`auth.users(id)` (UUID) is the FK for every table. Login is Sign in with Apple
(Face ID / Apple ID — no typed password, no CAPTCHA; the accessible path).
Supabase issues an HS256 JWT whose `sub` is the user UUID; Supabase handles
refresh. Endpoints get identity **only** via `Depends(get_current_user_id)` and
never decode tokens themselves — the dev/real swap touches only
`shared/auth.py`. `AUTH_MODE=dev` (default) resolves to a fixed dev UUID.
Verify logout/token-revocation through an actual VoiceOver pass, not just
functionally.

**Confirm-gate — one shared table for all modes.** `pending_actions`
(`source_mode`, `action_type`, `payload` jsonb, `status`
pending/confirmed/rejected/expired, `expires_at`). Irreversible actions
(send_email, create_event, reschedule_event; health log writes; manuscript
inserts) never execute directly — they create a pending row, get read back
aloud, and commit only on a matched spoken "yes" or explicit confirm. The
fails-closed spoken-yes/no matcher (`confirm_match.py` in the reference) is the
sole authority and ports verbatim into `shared/`. `expires_at` exists because a
missed-STT "yes" that lingers forever is a real bug.

**Sync — LWW by default, with one carve-out.** `health_entries` and general
log rows use last-write-wins + soft delete (`deleted_at`) — fine for
independent rows across one person's devices; no CRDT. **Writing is NOT LWW.**
Google Docs is the source of truth. Offline dictation is stored append-only per
session (`writing_sessions` + `writing_drafts`: device + session + seq +
text_delta) and merged into Docs by `batchUpdate` insertText at a saved anchor
— never a full-document overwrite (which would silently delete paragraphs
edited on the web meanwhile). `writing_documents.updated_at` is metadata only,
never a content-sync signal.

## Port source
The working reference is `Oliver_Jarvis_V2` (branch `master`) — a Python/FastAPI
app. Its agent loop, 14-tool set, confirm-gate, spoken read-back, and Google
client decompose into a shared execution layer (used by all modes) plus
per-mode toolsets. Do not rewrite `confirm_match.py` / `spoken_readback.py`.

## Build status
- [x] Auth injection (`shared/auth.py`), `/me`, `/devices` wired into the mode
      router; `user_id` removed from `/chat` body (identity from token).
- [x] Migrations `001_users_devices.sql` (Supabase), `002_core_schema.sql`
      (full schema above). **Drafted — not yet run against a database.**
- [ ] Wire devices/DB to Postgres (asyncpg), replace in-memory `_devices` stub.
- [ ] Real Supabase JWT verification (flip `AUTH_MODE=real`) + iOS Apple flow.
- [ ] Port the shared confirm-gate + agent loop + Jarvis tools.

## Open item
**Naming.** `lifesight-backend` / `lifesign-IOS` / persona "Olivia" /
"LifeSite"/"Jarvis" are all in play. Settle ONE canonical name with Jack before
it bakes into bundle IDs, API namespaces, and the `source_mode='jarvis'` enum.
