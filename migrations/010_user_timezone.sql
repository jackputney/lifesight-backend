-- 010: optional IANA timezone on self-hosted user profile (account-scoped, not device-local).
-- NULL means unset; existing rows remain valid. Validated in application code via zoneinfo.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS timezone TEXT;

COMMENT ON COLUMN users.timezone IS
    'Optional IANA timezone id (e.g. America/Los_Angeles). NULL = unset.';
