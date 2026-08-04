# Author Mode — build spec & merge reconciliation

Living doc for Author Mode (Google Docs) work on Clone B. Captures validation
results and concrete merge decisions so they survive past any single chat session.

## Track A — Google Docs API smoke test

**Script:** `scripts/track_a_google_docs.py`

**What it proves:** `shared/google_docs.py` can create a doc, read it, append
text, and read back — against the live Google API, with no Postgres and no
`shared/crypto.py`.

**How to run:**

```bash
./venv/bin/python scripts/track_a_google_docs.py
```

Either set `GOOGLE_ACCESS_TOKEN` or `GOOGLE_REFRESH_TOKEN` in `.env` (manual
token — never commit), or let the script open a browser using
`GOOGLE_REDIRECT_URI` from `.env.

**Result:** _(fill in after each run — date, PASS/FAIL, notes)_

| Date | Result | Notes |
|------|--------|-------|
| 2026-07-27 | BLOCKED (OAuth timeout) | Script + PKCE fix landed; first agent runs timed out or hit `Missing code verifier` (fixed). |
| 2026-07-27 | FAIL at `create_document` | OAuth + PKCE confirmed working. `HttpError 403 SERVICE_DISABLED` — enable **Google Docs API** (and **Drive API** for `drive.file`) in GCP project `239032834734`, wait ~2 min, re-run. Steps 2–4 not reached. **Checkpoint:** confirm `239032834734` is Jack's Author GCP project, not Oliver's Jarvis project — see below. |

---

## GCP project checkpoint (before enabling APIs)

The OAuth client in `.env` belongs to GCP project **`239032834734`**
(embedded in `GOOGLE_CLIENT_ID` as `239032834734-…apps.googleusercontent.com`).

The repo does **not** record the human-readable project name — only you can
confirm in [Google Cloud Console](https://console.cloud.google.com/) →
project switcher (top-left dropdown).

| If project `239032834734` is… | Action |
|-------------------------------|--------|
| **Your Author Mode project** | Enable Docs API + Drive API there, re-run Track A. |
| **Oliver's Jarvis project** | Do **not** enable Docs on his project. Create/use your own GCP project with a Web OAuth client, Docs + Drive APIs enabled, redirect URI `http://localhost:8000/oauth/google/callback`, and update `.env` `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`. |

**Why this matters:** Jarvis OAuth is scoped for Calendar/Gmail/People — not
Docs. A `SERVICE_DISABLED` on `docs.googleapis.com` is exactly what you'd see
if `.env` points at the wrong project's client. Same root cause class as an
earlier `redirect_uri_mismatch`, showing up differently.

**Circumstantial clue (not proof):** `.env` contains both Author-style
(`TOKEN_ENCRYPTION_KEY`) and jarvis-oauth-style (`GOOGLE_TOKEN_ENCRYPTION_KEY`,
`OAUTH_STATE_SECRET`) vars — worth verifying the Google client matches Author,
not a Jarvis handoff.

**Bug found during Track A (fixed):** `build_auth_url` and `exchange_code`
each created a fresh `Flow`, so PKCE's code verifier was lost between redirect
and callback (`Missing code verifier`). Fixed by stashing the pending `Flow`
keyed by OAuth `state` in `shared/google_docs.py`; `exchange_code` now requires
`state` as a second argument.

---

## Pre-merge reconciliation — `jarvis-oauth` vs Author work

Both branches fork from `e4a9a24` (`origin/main`). Before merging
`origin/jarvis-oauth` into Author work, resolve these three decisions explicitly.

### 1. OAuth upsert API — pick one name and return shape

| | jarvis-oauth | Author (uncommitted) |
|---|-------------|----------------------|
| Upsert fn | `upsert_oauth_credentials()` → returns full row | `save_oauth_credentials()` → returns `None` |
| Read fn | `get_oauth_credentials()` — SELECT includes `id`, `updated_at` | `get_oauth_credentials()` — slimmer SELECT |
| Delete fn | `delete_oauth_credentials()` | _(none)_ |

**Decision (TBD):** _Record chosen API here before merge._

**Recommendation:** Keep jarvis's `upsert_oauth_credentials` + `delete_oauth_credentials`
as canonical (Jarvis OAuth routes need delete; upsert returning the row is
useful). Add a thin `save_oauth_credentials` alias only if Author `main.py`
still calls that name at merge time — or update Author callers to `upsert_*`.
Merge to **one** `get_oauth_credentials` SELECT (include `id`, `updated_at`).

**Silent-breakage warning:** Do not leave two definitions of
`get_oauth_credentials` in the same file — Python uses the last one with no
error.

### 2. Non-overlapping `shared/db.py` additions — keep both

| Block | Source | Action |
|-------|--------|--------|
| Reminders CRUD (~line 220) | jarvis-oauth | Keep |
| `get_pending_action` expansion + payload JSON decode | Author | Keep (also requested in Jarvis Phase 3 proposal) |
| `writing_documents` helpers | Author | Keep |
| OAuth credential helpers | Both | Reconcile per decision 1 — do not duplicate |

### 3. `shared/crypto.py` env vars — align names before deploy

| | jarvis-oauth | Author (uncommitted) |
|---|-------------|----------------------|
| Token encryption key | `GOOGLE_TOKEN_ENCRYPTION_KEY` | `TOKEN_ENCRYPTION_KEY` |
| OAuth state HMAC | `OAUTH_STATE_SECRET` (in `shared/crypto.py`) | Inline in `main.py` (`_sign_oauth_state`) |

**Decision (TBD):** _Record chosen env var names and where state signing lives._

**Recommendation:** Adopt jarvis names (`GOOGLE_TOKEN_ENCRYPTION_KEY`,
`OAUTH_STATE_SECRET`) and move Author's inline state signing into
`shared/crypto.py` so one module owns all crypto. Update `.env` / Railway env
to match — a name mismatch fails at runtime with a confusing "key not set" error,
not at merge time.

### `shared/auth.py`

No changes on `jarvis-oauth`. No reconciliation needed.

---

## Related files touched by Author work (uncommitted on Clone B)

- `main.py` — OAuth routes, Author confirm-gate wiring
- `modes/author/prompt.py`
- `shared/db.py`, `shared/crypto.py`, `shared/google_docs.py`
- `requirements.txt`

## Clone A

Retired — plain fresh clone with nothing unique. No further comparison needed.
