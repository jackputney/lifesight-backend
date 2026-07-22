# Mobile API guide — Lifesight iOS ↔ backend

Companion to the frozen contract in `.cursor/rules/10-api-contract.mdc` and
`AGENTS.md`. The iOS app talks only to this backend — never to Claude or Google
directly.

Base URL (local): `http://127.0.0.1:8000`  
Auth header on every JSON request (except the Google callback browser hop):

```
Authorization: Bearer <supabase-jwt-or-any-string-in-dev>
```

In `AUTH_MODE=dev` (default), any Bearer value — or none — resolves to the fixed
dev user. In `AUTH_MODE=real`, send the Supabase session JWT.

---

## Endpoints at a glance

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | no | Liveness |
| GET | `/modes` | no | `{"modes":["author","health","jarvis"]}` |
| GET | `/me` | Bearer | Resolved `{user_id}` |
| POST | `/chat` | Bearer | Voice turn → reply + optional pending_action |
| POST | `/confirm` | Bearer | Confirm Gate second half |
| POST/GET/DELETE | `/devices`… | Bearer | Push-token registration |
| GET | `/oauth/google/authorize` | Bearer | `{authorization_url}` to open in Safari |
| GET | `/oauth/google/callback` | none (browser) | Google redirect; HTML success page |

Shapes for `/chat`, `/confirm`, `/devices` match `10-api-contract.mdc` exactly —
do not invent fields on either side.

---

## Connect Google (Jarvis)

SFSafariViewController / ASWebAuthenticationSession **cannot** attach an
`Authorization` header to a navigation. A Bearer-gated `302` from
`/oauth/google/authorize` would never work under real auth. The contract is
therefore:

1. **App** calls `GET /oauth/google/authorize` as a normal authenticated JSON
   request (URLSession with Bearer).
2. **Backend** returns `200 {"authorization_url": "https://accounts.google.com/..."}`.
   The URL already includes a signed `state` (HMAC, `OAUTH_STATE_SECRET`) that
   embeds `user_id` + expiry.
3. **App** opens `authorization_url` in SFSafariViewController (or
   ASWebAuthenticationSession). No auth header on that hop.
4. User consents on Google’s screen. Google redirects the browser to
   `GET /oauth/google/callback?code=...&state=...`.
5. **Backend** verifies `state`, exchanges `code`, Fernet-encrypts tokens
   (`GOOGLE_TOKEN_ENCRYPTION_KEY`), upserts `oauth_credentials` for
   `(user_id, "google")`, and returns a short HTML page:
   “Google connected. You can close this window and return to Lifesight.”
6. **App** dismisses the browser session when the user returns (or when the
   success page loads, if using ASWebAuthenticationSession with a custom scheme
   later — not required for this phase).

If the user denies consent, callback returns `400` with a readable message.
If `state` is missing, forged, or expired (~10 minutes), callback returns `400`
and stores nothing.

Do **not** put the Supabase JWT in the authorize URL query string. Do **not**
send `user_id` in any request body — identity is always server-derived.

---

## Confirm Gate (voice-critical)

Irreversible actions (send email, create/modify calendar, write manuscript,
save health log) never execute on the first `/chat` pass:

1. `/chat` returns `pending_action: {action_id, description}` — speak `description`.
2. User confirms → `POST /confirm {"action_id", "approved": true}`.
3. User cancels → `approved: false` (nothing executes).

---

## Not in this guide yet

- Deep-link callback (`lifesight://oauth/...`) — callback is HTML-only for now.
- Status/disconnect endpoints for Google — reconnect by running authorize again
  (upsert replaces the row).
