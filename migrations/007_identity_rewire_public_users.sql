-- 007: Rewire domain ownership from auth.users → public.users
--
-- Context: self-hosted auth (006) introduced public.users. Domain tables still
-- FK'd Supabase auth.users, so AUTH_MODE=self chat/create failed with FK errors.
--
-- Strategy (transactional):
--   1) Insert inactive "legacy_*" stub rows in public.users for any domain
--      user_id not already present (preserves existing rows; no deletes).
--   2) Ensure the AUTH_MODE=dev fixed UUID exists in public.users.
--   3) Drop every public.* FK that references auth.users(id).
--   4) Re-add those FKs pointing at public.users(id) ON DELETE CASCADE.
--
-- Unmapped / stubbed identities:
--   Rows whose user_id is not a registered self-hosted account get an inactive
--   public.users stub (username l<32 hex uuid chars>, is_active=false, unusable
--   Argon2id hash). They cannot log in until an admin remaps or activates them.
--   Count stubs created: see NOTICE output / SELECT after migrate.
--
-- ROLLBACK NOTES (manual; do not auto-run):
--   BEGIN;
--   -- Drop public.users FKs added below (same constraint names).
--   -- Re-add FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE
--   --   for each table listed in this file.
--   -- Optionally DELETE FROM users WHERE username LIKE 'l%' AND length(username)=33
--   --   AND display_name = 'Legacy preserved account' AND is_active = false;
--   -- Do NOT delete the DEV_FAKE_USER_ID row if AUTH_MODE=dev still needs it.
--   COMMIT;
--   WARNING: rollback only works if every user_id still exists in auth.users.
--   Stub-only ids that never existed in auth.users will block re-adding those FKs.

BEGIN;

-- Unusable Argon2id hash (password not known; stubs cannot authenticate).
-- pragma: allowlist secret
CREATE TEMP TABLE _identity_rewire_const (
    stub_password_hash TEXT NOT NULL
);
INSERT INTO _identity_rewire_const (stub_password_hash) VALUES (
    '$argon2id$v=19$m=65536,t=3,p=4$PvirOBKxPzgqJveZrS8AFA$L1MWnHZ/QAMJI4eOjfP0e/L07+Y5qeYs2k9Mep0rjA0'  -- pragma: allowlist secret
);

-- Collect distinct ownership ids from every table that still FKs auth.users.
CREATE TEMP TABLE _domain_user_ids AS
SELECT user_id AS id FROM action_log WHERE user_id IS NOT NULL
UNION SELECT user_id FROM conversations WHERE user_id IS NOT NULL
UNION SELECT user_id FROM daily_nutrition_targets WHERE user_id IS NOT NULL
UNION SELECT user_id FROM devices WHERE user_id IS NOT NULL
UNION SELECT user_id FROM food_entries WHERE user_id IS NOT NULL
UNION SELECT user_id FROM health_entries WHERE user_id IS NOT NULL
UNION SELECT user_id FROM health_metrics WHERE user_id IS NOT NULL
UNION SELECT user_id FROM health_plans WHERE user_id IS NOT NULL
UNION SELECT user_id FROM manuscripts WHERE user_id IS NOT NULL
UNION SELECT user_id FROM memories WHERE user_id IS NOT NULL
UNION SELECT user_id FROM oauth_credentials WHERE user_id IS NOT NULL
UNION SELECT user_id FROM pending_actions WHERE user_id IS NOT NULL
UNION SELECT user_id FROM personal_records WHERE user_id IS NOT NULL
UNION SELECT user_id FROM reminders WHERE user_id IS NOT NULL
UNION SELECT user_id FROM sync_state WHERE user_id IS NOT NULL
UNION SELECT user_id FROM wearable_connections WHERE user_id IS NOT NULL
UNION SELECT user_id FROM workout_plans WHERE user_id IS NOT NULL
UNION SELECT user_id FROM workout_sessions WHERE user_id IS NOT NULL
UNION SELECT user_id FROM writing_documents WHERE user_id IS NOT NULL;

-- Optional tables that may exist on some databases (mail-calendar / other WIP).
DO $$
BEGIN
    IF to_regclass('public.oauth_transactions') IS NOT NULL THEN
        INSERT INTO _domain_user_ids (id)
        SELECT DISTINCT ot.user_id
        FROM oauth_transactions ot
        WHERE ot.user_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM _domain_user_ids d WHERE d.id = ot.user_id);
    END IF;
    IF to_regclass('public.profiles') IS NOT NULL THEN
        INSERT INTO _domain_user_ids (id)
        SELECT DISTINCT p.user_id
        FROM profiles p
        WHERE p.user_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM _domain_user_ids d WHERE d.id = p.user_id);
    END IF;
END $$;

-- AUTH_MODE=dev fixed UUID (shared.auth.DEV_FAKE_USER_ID) — before stubs.
INSERT INTO users (id, username, email, password_hash, display_name, is_active)
SELECT
    '00000000-0000-4000-8000-000000000001'::uuid,
    'dev_local',
    NULL,
    c.stub_password_hash,
    'Dev bypass user',
    TRUE
FROM _identity_rewire_const c
ON CONFLICT (id) DO NOTHING;

CREATE TEMP TABLE _orphan_user_ids AS
SELECT DISTINCT d.id
FROM _domain_user_ids d
LEFT JOIN users u ON u.id = d.id
WHERE u.id IS NULL
  AND d.id IS NOT NULL
  AND d.id <> '00000000-0000-4000-8000-000000000001'::uuid;

INSERT INTO users (id, username, email, password_hash, display_name, is_active)
SELECT
    o.id,
    -- Full 32-hex id keeps usernames unique (truncated prefixes can collide).
    'l' || replace(o.id::text, '-', ''),
    NULL,
    c.stub_password_hash,
    'Legacy preserved account',
    FALSE
FROM _orphan_user_ids o
CROSS JOIN _identity_rewire_const c
ON CONFLICT (id) DO NOTHING;

-- Drop auth.users foreign keys (constraint names from original migrations / live DB).
ALTER TABLE action_log DROP CONSTRAINT IF EXISTS action_log_user_id_fkey;
ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_user_id_fkey;
ALTER TABLE daily_nutrition_targets DROP CONSTRAINT IF EXISTS daily_nutrition_targets_user_id_fkey;
ALTER TABLE devices DROP CONSTRAINT IF EXISTS devices_user_id_fkey;
ALTER TABLE food_entries DROP CONSTRAINT IF EXISTS food_entries_user_id_fkey;
ALTER TABLE health_entries DROP CONSTRAINT IF EXISTS health_entries_user_id_fkey;
ALTER TABLE health_metrics DROP CONSTRAINT IF EXISTS health_metrics_user_id_fkey;
ALTER TABLE health_plans DROP CONSTRAINT IF EXISTS health_plans_user_id_fkey;
ALTER TABLE manuscripts DROP CONSTRAINT IF EXISTS manuscripts_user_id_fkey;
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_user_id_fkey;
ALTER TABLE oauth_credentials DROP CONSTRAINT IF EXISTS oauth_credentials_user_id_fkey;
ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS pending_actions_user_id_fkey;
ALTER TABLE personal_records DROP CONSTRAINT IF EXISTS personal_records_user_id_fkey;
ALTER TABLE reminders DROP CONSTRAINT IF EXISTS reminders_user_id_fkey;
ALTER TABLE sync_state DROP CONSTRAINT IF EXISTS sync_state_user_id_fkey;
ALTER TABLE wearable_connections DROP CONSTRAINT IF EXISTS wearable_connections_user_id_fkey;
ALTER TABLE workout_plans DROP CONSTRAINT IF EXISTS workout_plans_user_id_fkey;
ALTER TABLE workout_sessions DROP CONSTRAINT IF EXISTS workout_sessions_user_id_fkey;
ALTER TABLE writing_documents DROP CONSTRAINT IF EXISTS writing_documents_user_id_fkey;

DO $$
BEGIN
    IF to_regclass('public.oauth_transactions') IS NOT NULL THEN
        ALTER TABLE oauth_transactions DROP CONSTRAINT IF EXISTS oauth_transactions_user_id_fkey;
    END IF;
    IF to_regclass('public.profiles') IS NOT NULL THEN
        ALTER TABLE profiles DROP CONSTRAINT IF EXISTS profiles_user_id_fkey;
    END IF;
END $$;

-- Point ownership at public.users
ALTER TABLE action_log
    ADD CONSTRAINT action_log_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE conversations
    ADD CONSTRAINT conversations_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE daily_nutrition_targets
    ADD CONSTRAINT daily_nutrition_targets_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE devices
    ADD CONSTRAINT devices_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE food_entries
    ADD CONSTRAINT food_entries_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE health_entries
    ADD CONSTRAINT health_entries_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE health_metrics
    ADD CONSTRAINT health_metrics_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE health_plans
    ADD CONSTRAINT health_plans_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE manuscripts
    ADD CONSTRAINT manuscripts_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE memories
    ADD CONSTRAINT memories_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE oauth_credentials
    ADD CONSTRAINT oauth_credentials_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE pending_actions
    ADD CONSTRAINT pending_actions_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE personal_records
    ADD CONSTRAINT personal_records_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE reminders
    ADD CONSTRAINT reminders_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE sync_state
    ADD CONSTRAINT sync_state_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE wearable_connections
    ADD CONSTRAINT wearable_connections_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE workout_plans
    ADD CONSTRAINT workout_plans_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE workout_sessions
    ADD CONSTRAINT workout_sessions_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE writing_documents
    ADD CONSTRAINT writing_documents_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;

DO $$
BEGIN
    IF to_regclass('public.oauth_transactions') IS NOT NULL THEN
        ALTER TABLE oauth_transactions
            ADD CONSTRAINT oauth_transactions_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
    END IF;
    IF to_regclass('public.profiles') IS NOT NULL THEN
        ALTER TABLE profiles
            ADD CONSTRAINT profiles_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
    END IF;
END $$;

DO $$
DECLARE
    stub_count INTEGER;
BEGIN
    SELECT count(*) INTO stub_count FROM users
    WHERE display_name = 'Legacy preserved account' AND is_active = FALSE;
    RAISE NOTICE 'identity rewire complete; legacy stub users now present: %', stub_count;
END $$;

COMMIT;
