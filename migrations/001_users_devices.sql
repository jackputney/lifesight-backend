-- 001_users_devices.sql
-- Identity decision (FROZEN): Supabase Auth owns identity. user_id is the
-- UUID from auth.users(id), and it is the FK for every table. Login is
-- Sign in with Apple (accessibility — Face ID / Apple ID, no typed password),
-- so there is NO users table and NO password column anywhere in this schema.
--
-- This migration runs inside a Supabase project, where the `auth` schema and
-- auth.users already exist.

-- devices — push-notification targets per user ------------------------------
CREATE TABLE IF NOT EXISTS devices (
    device_id  TEXT NOT NULL,              -- client-generated stable ID (iOS identifierForVendor)
    user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    push_token TEXT,
    platform   TEXT NOT NULL DEFAULT 'ios'
               CHECK (platform IN ('ios', 'android', 'web')),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_devices_user ON devices (user_id);

-- Local-dev seed (OPTIONAL, not run automatically) --------------------------
-- AUTH_MODE=dev resolves every request to the fixed UUID below. To exercise
-- FKs against a real database you need a matching auth.users row. On a real
-- Supabase project, create it by signing in once with Apple (preferred), or
-- insert a minimal dev row as the postgres role:
--
--   INSERT INTO auth.users (id, aud, role, email)
--   VALUES ('00000000-0000-4000-8000-000000000001',
--           'authenticated', 'authenticated', 'dev@local.test')
--   ON CONFLICT (id) DO NOTHING;
--
-- (Exact required columns vary by Supabase version — adjust as needed. Kept
-- out of the automatic migration so it never touches a production auth table.)
