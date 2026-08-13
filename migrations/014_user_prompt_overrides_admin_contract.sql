-- 014: user_prompt_overrides + formalize account-status metadata for Oliver admin.
-- No /admin HTTP surface. Oliver's separate admin project writes these tables directly.
-- LifeSight backend is the consumer of active overrides and is_active at runtime.

-- ---------------------------------------------------------------------------
-- Account status (reuse users.is_active; add admin metadata only)
-- ---------------------------------------------------------------------------
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS status_reason TEXT,
    ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS status_changed_by TEXT;

COMMENT ON COLUMN users.is_active IS
    'Account enablement (source of truth). false = disabled: login, refresh, '
    'and /auth/me fail; every authenticated request rejects the access JWT. '
    'Oliver admin may UPDATE this column directly. Prefer also revoking '
    'auth_sessions when disabling.';
COMMENT ON COLUMN users.status_reason IS
    'Optional admin note for the latest enable/disable action.';
COMMENT ON COLUMN users.status_changed_at IS
    'When is_active / status_reason last changed by admin tooling.';
COMMENT ON COLUMN users.status_changed_by IS
    'Admin actor identifier (email/username/service), not an FK.';

-- ---------------------------------------------------------------------------
-- Versioned per-user prompt customization (LifeSight reads active rows only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_prompt_overrides (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mode            TEXT,
    instructions    TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    is_active       BOOLEAN NOT NULL DEFAULT FALSE,
    reason          TEXT,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_prompt_overrides_instructions_nonempty CHECK (
        char_length(btrim(instructions)) > 0
    ),
    CONSTRAINT user_prompt_overrides_instructions_maxlen CHECK (
        char_length(instructions) <= 8000
    ),
    CONSTRAINT user_prompt_overrides_version_positive CHECK (version >= 1),
    CONSTRAINT user_prompt_overrides_mode_chk CHECK (
        mode IS NULL
        OR mode IN (
            'fitness',
            'diet',
            'author',
            'brainstorm',
            'mail_calendar',
            'jarvis',
            'checkin'
        )
    )
);

COMMENT ON TABLE user_prompt_overrides IS
    'Admin-managed per-user prompt customization. mode NULL = global. '
    'LifeSight loads only is_active rows and treats them as subordinate to '
    'IDENTITY / epistemic / feasibility / mode instructions.';
COMMENT ON COLUMN user_prompt_overrides.mode IS
    'NULL = global customization for all modes; otherwise a MODE_REGISTRY key.';
COMMENT ON COLUMN user_prompt_overrides.version IS
    'Monotonic version for this user+mode lineage (admin-managed).';
COMMENT ON COLUMN user_prompt_overrides.is_active IS
    'At most one active row per (user_id, mode) including NULL mode.';
COMMENT ON COLUMN user_prompt_overrides.created_by IS
    'Admin actor who wrote the row (not an FK).';

-- Exactly one active override per (user_id, mode-or-global).
CREATE UNIQUE INDEX IF NOT EXISTS user_prompt_overrides_one_active_per_user_mode
    ON user_prompt_overrides (user_id, (COALESCE(mode, '')))
    WHERE is_active;

CREATE INDEX IF NOT EXISTS user_prompt_overrides_user_active_idx
    ON user_prompt_overrides (user_id)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS user_prompt_overrides_user_mode_version_idx
    ON user_prompt_overrides (user_id, mode, version DESC);

-- ---------------------------------------------------------------------------
-- Reuse admin_audit_log as Oliver's audit-events store (no duplicate table)
-- ---------------------------------------------------------------------------
COMMENT ON TABLE admin_audit_log IS
    'Admin audit events for seed scripts and Oliver admin project. '
    'Preferred write shape: actor, action, target_user_id, detail JSONB, created_at. '
    'Do not store passwords, tokens, or full sensitive record dumps in detail.';
COMMENT ON COLUMN admin_audit_log.actor IS
    'Who performed the action (admin user/service id or email).';
COMMENT ON COLUMN admin_audit_log.action IS
    'Stable verb, e.g. disable_user, enable_user, upsert_prompt_override, '
    'deactivate_prompt_override, upsert_user_profile.';
COMMENT ON COLUMN admin_audit_log.target_user_id IS
    'Affected LifeSight user, if any.';
COMMENT ON COLUMN admin_audit_log.detail IS
    'Metadata only: changed_fields[], reason, mode, version, before/after '
    'summaries. Avoid duplicating full PII or secrets.';

-- Compatibility view for Oliver tooling that prefers "admin_audit_events".
CREATE OR REPLACE VIEW admin_audit_events AS
SELECT
    id,
    actor,
    action,
    target_user_id,
    detail,
    created_at
FROM admin_audit_log;
