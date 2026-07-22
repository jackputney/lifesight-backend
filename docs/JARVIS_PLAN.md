# Jarvis (Olivia) build plan — status and remaining work

Reference implementation: `Odugan405/Oliver_Jarvis_V2` (branch `master`),
cloned as a sibling folder. `confirm_match.py` / `spoken_readback.py` ported
verbatim — never rewrite them. Everything here happens on the `jarvis-oauth`
branch per `docs/OWNERSHIP.md`.

## Done
- [x] **Phase 0** — Google Cloud Web-application OAuth client (Calendar, Gmail,
      People APIs enabled; External consent screen; Oliver as test user).
      Credentials live in `.env` only.
- [x] **Phase 1** — Confirm-gate primitives: `shared/confirm_match.py`,
      `shared/spoken_readback.py` (verbatim ports), `create_pending_action` /
      `save_memory` / `recall_memories` / `log_action` in `shared/db.py`.
      Merged to main (e4a9a24) before the branch-only rule; everything after
      lives on the branch.
- [x] **Phase 2** — `shared/agent_loop.py` (mode-agnostic, async dispatch) +
      `modes/jarvis/tools.py` with the four non-Google tools. Reminder rows
      store/list/cancel but nothing fires them (no scheduler, no push channel
      to deliver to — deliberate, documented gap).
- [x] **Phase 4 (code)** — `shared/crypto.py`, `shared/google_client.py`
      (per-user, DB-backed tokens), `/oauth/google/authorize` (Bearer JSON →
      `{authorization_url}`) and `/oauth/google/callback` (signed state,
      option-A HTML). Contract docs + `MOBILE_API_GUIDE.md` updated in lockstep.

## Next (in order)
- [ ] **Add nonce to OAuth state** — Jack's approval specified HMAC of
      user_id + nonce + expiry; current state is user_id + expiry only.
      Small change in `shared/crypto.py`, no interface change.
- [ ] **End-to-end OAuth test** — run the server, GET /oauth/google/authorize
      (dev auth), complete consent with the TEST Google account (never Jock's
      real account, per AGENTS.md), verify encrypted row lands in
      `oauth_credentials`, verify refresh works after expiry.
- [ ] **Phase 5** — the ten Google-backed tools in `modes/jarvis/tools.py`
      (schemas + dispatch from the reference's `app/tools.py`, calling
      `shared/google_client.py`). The three irreversible ones
      (`create_calendar_event`, `send_email`, `reschedule_event`) create
      pending_actions via `db.create_pending_action` — never execute directly.
      Include `execute_confirmed_action` for the /confirm side.
- [ ] **WAIT FOR JACK, then Phase 3** — wiring `/chat` to the agent loop and
      `/confirm` to the executor is main.py + Confirm Gate internals = Jack's
      lane. He said he'll come back with specific integration asks once his
      gate reconciliation is done. Prepare a proposal; do not implement first.
- [ ] **Phase 6 (with Jack)** — voice-confirm endpoints
      (`/spoken-readback`, `/match-confirmation`) — contract additions,
      same proposal-first process.
- [ ] **Later / tracked** — reminder firing + push delivery (needs APNs
      via the devices table); rotating the DB password and Google client
      secret (both transited chat on 2026-07-20/22); deep-link callback
      (option B) when iOS builds the connect button.

## Standing constraints (from AGENTS.md / 00-core.mdc — repeated because they
keep almost getting violated)
- Both repos are PUBLIC. No real names, health data, tokens, or secrets in
  code, comments, commits, or docs. `.env` only.
- Jarvis development uses a TEST Google account until a supervised
  integration test.
- The Confirm Gate is never bypassed, folded into the executor, or skipped
  for "obviously safe" actions.
- Frozen API contract: changes are cross-repo breaking changes — propose,
  don't edit.
