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
- [x] **Add nonce to OAuth state** — HMAC of `user_id` + nonce + expiry in
      `shared/crypto.py`. No external API change (state stays opaque).
- [x] **OAuth path smoke test** — authorize URL + signed state verify via
      TestClient; Fernet encrypt/decrypt; DB reachable. Interactive Google
      consent with the TEST account (row in `oauth_credentials` + forced
      refresh) still needs a manual browser pass — no credentials row yet.
- [x] **Phase 5** — ten Google-backed tools in `modes/jarvis/tools.py`
      (schemas + dispatch from the reference, calling `shared/google_client.py`).
      Irreversible three create `pending_actions` via `db.create_pending_action`;
      `execute_confirmed_action` ready for the `/confirm` side.
- [x] **Phase 3 proposal** — written for Jack at
      `docs/JARVIS_PHASE3_PROPOSAL.md`. Do not implement wiring until he says go.
- [x] **Scope union on re-consent** — `save_credentials` now unions newly
      granted scopes with the row's existing set instead of overwriting, so a
      narrower reconnect on one mode can't drop another mode's access (e.g.
      Jarvis reconnecting must not strip Author's future Docs scope). Done in
      `google_client.py`, not the upsert SQL, to stay out of Jack's `db.py`.

## Open question with Jack
- **`oauth_credentials.provider` shape** (he asked 2026-07-22): vendor-level
  (one `google` row per user, merged scope superset) vs. per-feature rows.
  Recommended answer: vendor-level — it's what the table comment ("under one
  grant"), the `UNIQUE (user_id, provider)` constraint, and the current code
  (`PROVIDER = "google"`, `include_granted_scopes=true`) already assume, and it
  means one consent screen for a hands-free user instead of one per feature.
  The scope-union fix above is safe under either answer. Follow-up for him:
  the canonical `SCOPES` list currently lives in Jarvis's `google_client.py`
  and has no Docs scope — when Author-Docs is built, that shared union needs a
  home neither lane has to reach across to edit.

## Next (in order)
- [ ] **Manual OAuth consent (TEST account)** — GET `/oauth/google/authorize`,
      complete Google consent, confirm encrypted `oauth_credentials` row, force
      refresh after local expiry. Never use Jock's real account.
- [ ] **WAIT FOR JACK, then Phase 3** — wire `/chat` → agent loop and
      `/confirm` → `execute_confirmed_action` per the proposal. Jack's lane.
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
