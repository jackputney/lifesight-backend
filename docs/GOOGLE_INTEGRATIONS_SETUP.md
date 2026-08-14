# Google Integrations setup (per-user OAuth)

One LifeSight Google Cloud OAuth **application**. Every LifeSight user
connects **their own** Google account. There is never a shared LifeSight
Google identity used by all users.

Ownership:

```
authenticated LifeSight user
  → that user's google_connections row
  → that user's Google access only
```

Do not paste client secrets into chat. Configure them in Railway / local `.env`
only (see `.env.example` placeholders).

## Google Cloud checklist

1. Create (or reuse) a Google Cloud project for LifeSight.
2. Enable APIs:
   - Google Calendar API
   - Gmail API (for later `gmail_send` / `gmail_read`; not required for calendar-only beta)
   - People API / OpenID (userinfo) as needed for identity
3. Configure OAuth consent screen (External or Internal as appropriate).
4. Add test users while the app is in Testing.
5. Create OAuth client type **Web application**.
6. Authorized redirect URI (backend callback — exact path):

   ```
   https://<your-api-host>/integrations/google/callback
   ```

   Local development example:

   ```
   http://127.0.0.1:8000/integrations/google/callback
   ```

7. iOS opens `authorization_url` via `ASWebAuthenticationSession`, then
   receives the app return URI with **only** `result=success|error|reauth_required`.
   Put that app URI on `GOOGLE_APP_RETURN_URI_ALLOWLIST` (no query/fragment).

## Progressive capabilities

`POST /integrations/google/start` accepts **capability names**, never raw
scope URLs. Backend maps:

| Capability | Scopes (fixed allowlist) | TestFlight default |
|---|---|---|
| `google_identity` | `openid`, userinfo.email, userinfo.profile | yes (always) |
| `calendar` | `calendar.events` | yes |
| `gmail_send` | `gmail.send` | no (prepared) |
| `gmail_read` | `gmail.readonly` | no (prepared) |

Default when `capabilities` is omitted: `google_identity` + `calendar`.

## Server secrets (placeholders only)

```
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_INTEGRATIONS_REDIRECT_URI=https://api.example/integrations/google/callback
GOOGLE_APP_RETURN_URI_ALLOWLIST=lifesight://google-oauth
GOOGLE_OAUTH_ENV=production
TOKEN_ENCRYPTION_KEY=   # Fernet key — NOT AUTH_JWT_SECRET
```

`TOKEN_ENCRYPTION_KEY` encrypts refresh tokens and PKCE verifiers at rest
(`shared/crypto.py`). Do not reuse JWT signing material.

## Frozen HTTP contract

- `GET /integrations/google/status` → `{connected, email, capabilities}`
- `POST /integrations/google/start` → `{authorization_url, expires_in}`
- `GET /integrations/google/callback` → 302 to app return with `result=`
- `POST /integrations/google/disconnect` → `{disconnected: true}`

Tokens never appear in API responses or app redirects.

Abandoned Docs-era routes `/oauth/google/start|callback` remain **410** and
are not the integrations architecture.

## Confirm Gate

Calendar/email **reads** may run in chat tools immediately. External
**writes** (create/update/delete event, send email) stage `pending_action`
and execute only after `POST /confirm` with `approved: true`.

## Oliver

Oliver may inspect `google_connections` metadata (`google_email`, scopes,
timestamps, `revoked_at`). Oliver must never need decrypted refresh tokens.
