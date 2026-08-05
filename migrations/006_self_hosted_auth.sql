-- 006: self-hosted username/password users + refresh sessions
-- public.users is the identity store for AUTH_MODE=self (not Supabase auth.users).
-- Domain tables still FK auth.users until a later migration rewires them.

CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        TEXT NOT NULL,
    email           TEXT,
    password_hash   TEXT NOT NULL,
    display_name    TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_username_nonempty CHECK (char_length(username) >= 3),
    CONSTRAINT users_username_normalized CHECK (username = lower(username)),
    CONSTRAINT users_email_normalized CHECK (email IS NULL OR email = lower(email))
);

CREATE UNIQUE INDEX IF NOT EXISTS users_username_uidx ON users (username);
CREATE UNIQUE INDEX IF NOT EXISTS users_email_uidx ON users (email) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS auth_sessions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    refresh_token_hash   TEXT NOT NULL,
    expires_at           TIMESTAMPTZ NOT NULL,
    revoked_at           TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    device_name          TEXT
);

CREATE INDEX IF NOT EXISTS auth_sessions_user_id_idx ON auth_sessions (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS auth_sessions_refresh_hash_uidx
    ON auth_sessions (refresh_token_hash);

COMMENT ON TABLE users IS
    'Self-hosted accounts. password_hash is Argon2id; never store plaintext.';
COMMENT ON TABLE auth_sessions IS
    'Refresh sessions. Only SHA-256 hashes of refresh tokens are stored.';
