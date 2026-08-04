-- 005: temporary OAuth transactions for Mail & Calendar (PKCE + single-use state)
-- Separate from persistent oauth_credentials. Rows are short-lived.

CREATE TABLE IF NOT EXISTS oauth_transactions (
    state              TEXT PRIMARY KEY,
    user_id            UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    provider           TEXT NOT NULL,
    code_verifier_enc  TEXT NOT NULL,
    app_return_uri     TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ NOT NULL,
    consumed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS oauth_transactions_expires_idx
    ON oauth_transactions (expires_at);

COMMENT ON TABLE oauth_transactions IS
    'Ephemeral OAuth state/PKCE material. Delete or mark consumed after use; never store access/refresh tokens here.';
