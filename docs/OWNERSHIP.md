# Ownership — who owns what (settled 2026-07-22)

Agreed by Jack (message to Oliver, 2026-07-22) after shared-file collisions.
This is the routing table for every human and every coding agent on this
project. If a change doesn't fit your lane, it's a request to the lane owner,
not an edit.

## Oliver's lane — Jarvis Mode, end to end
- `modes/jarvis/**` (prompt, tools, everything)
- `shared/google_client.py`, `shared/crypto.py` — Jarvis-specific despite
  living in `shared/` (placement can be revisited at merge; Jack's call)
- The Google OAuth surface: `/oauth/google/*` endpoints, scopes, token storage
- Jarvis-related docs (`MOBILE_API_GUIDE.md` Google-connect section, this
  folder's plan docs)

**Branch discipline: ALL Oliver-lane work happens on `jarvis-oauth` (or a
successor `jarvis-*` branch). Never commit to `main`. Jack merges when the
shared layer is stable.**

## Jack's lane — everything else
- `shared/db.py`, `shared/auth.py`, `shared/agent_loop.py` (shared foundation)
- Confirm Gate internals (`shared/confirm_match.py`, `shared/spoken_readback.py`,
  `pending_actions` handling in `main.py`)
- `main.py` route wiring, `modes/author/**`, `modes/health/**`
- Migrations, `AGENTS.md`, `.cursor/rules/*` (except the Jarvis-lane rule)
- The entire `lifesight-ios` repo
- Jack runs his own Supabase project; Oliver's `DATABASE_URL` is Oliver's dev DB

## Cross-lane changes (either direction)
1. Write the proposal (shapes, files touched, why) — in the PR description or
   a `docs/` note, not straight into the other lane's files.
2. Wait for the lane owner's explicit yes.
3. Docs move in lockstep with code in the same PR: `10-api-contract.mdc`,
   `AGENTS.md` summary, and `MOBILE_API_GUIDE.md` — never "code now, doc later."

## Standing exceptions already granted
- Jack approved the OAuth endpoint shapes, callback option A, signed state
  (HMAC of user_id + nonce + expiry — nonce still TODO), and full
  Calendar/Gmail/People scopes (2026-07-22). The lockstep-docs condition applies.
- `shared/db.py` currently contains Jarvis-added reminders CRUD (branch only).
  Flagged for Jack at merge: keep there, or relocate to a Jarvis-owned module.
