-- 015: Per-user Google connections + ephemeral OAuth transactions.
--
-- One LifeSight Google OAuth app; each LifeSight user connects their own
-- Google account. Refresh tokens are Fernet-encrypted at the app layer
-- (TOKEN_ENCRYPTION_KEY via shared/crypto.py) — never returned via APIs.
--
-- Does NOT reuse legacy oauth_credentials (Docs-era, no google_subject /
-- revoked_at). Abandoned /oauth/google/* Docs routes stay 410.
--
-- V1 enforces one active connection per user via partial unique index.
-- Schema still allows multiple historical rows (and future multi-account
-- by relaxing the active-only unique).

CREATE TABLE IF NOT EXISTS google_connections (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    google_subject           TEXT NOT NULL,
    google_email             TEXT,
    display_name             TEXT,
    encrypted_refresh_token  TEXT NOT NULL,
    granted_scopes           TEXT[] NOT NULL DEFAULT '{}',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at               TIMESTAMPTZ,
    last_refresh_at          TIMESTAMPTZ
);

-- V1: at most one non-revoked Google account per LifeSight user.
CREATE UNIQUE INDEX IF NOT EXISTS google_connections_one_active_per_user
    ON google_connections (user_id)
    WHERE revoked_at IS NULL;

-- Subject is the security identity anchor within a user; email is metadata.
CREATE UNIQUE INDEX IF NOT EXISTS google_connections_user_subject_uidx
    ON google_connections (user_id, google_subject);

CREATE INDEX IF NOT EXISTS google_connections_user_id_idx
    ON google_connections (user_id);

COMMENT ON TABLE google_connections IS
    'Per-user Google OAuth connections. encrypted_refresh_token is Fernet ciphertext; never expose tokens via HTTP. Oliver may read metadata only.';
COMMENT ON COLUMN google_connections.google_subject IS
    'Google account subject (sub) from OpenID — security identity. Email is display metadata only.';
COMMENT ON COLUMN google_connections.encrypted_refresh_token IS
    'Fernet ciphertext of the refresh token. Decrypted only in-process for token refresh.';
COMMENT ON COLUMN google_connections.granted_scopes IS
    'OAuth scopes actually granted by Google for this connection.';

CREATE TABLE IF NOT EXISTS google_oauth_transactions (
    state                    TEXT PRIMARY KEY,
    user_id                  UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    code_verifier_enc        TEXT NOT NULL,
    app_return_uri           TEXT NOT NULL,
    requested_capabilities   TEXT[] NOT NULL DEFAULT '{}',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at               TIMESTAMPTZ NOT NULL,
    consumed_at              TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS google_oauth_transactions_expires_idx
    ON google_oauth_transactions (expires_at);

CREATE INDEX IF NOT EXISTS google_oauth_transactions_user_id_idx
    ON google_oauth_transactions (user_id);

COMMENT ON TABLE google_oauth_transactions IS
    'Ephemeral OAuth state + PKCE verifier for /integrations/google. Short-lived, single-use, bound to authenticated LifeSight user_id. Never store access/refresh tokens here.';
