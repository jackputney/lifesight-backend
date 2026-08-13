-- 012: richer user_profiles (1:1 with users) + admin_audit_log for seed tooling.
-- Gaps 010/011 reserved for other draft PRs. Identity fields stay on users.

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id                      UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    timezone                     TEXT,
    date_of_birth                DATE,
    height_cm                    NUMERIC(6, 2),
    weight_kg                    NUMERIC(6, 2),
    interaction_style            TEXT,
    vision_preference            TEXT,
    spoken_response_preference   TEXT,
    experience_level             TEXT,
    primary_goals                JSONB NOT NULL DEFAULT '[]'::jsonb,
    training_frequency           TEXT,
    available_equipment          JSONB NOT NULL DEFAULT '[]'::jsonb,
    injuries_limitations         TEXT,
    nutrition_goal               TEXT,
    dietary_preferences          JSONB NOT NULL DEFAULT '[]'::jsonb,
    allergies_restrictions       JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_profiles_interaction_style_chk CHECK (
        interaction_style IS NULL
        OR interaction_style IN ('standard', 'voice_first', 'high_accessibility')
    ),
    CONSTRAINT user_profiles_primary_goals_is_array CHECK (
        jsonb_typeof(primary_goals) = 'array'
    ),
    CONSTRAINT user_profiles_equipment_is_array CHECK (
        jsonb_typeof(available_equipment) = 'array'
    ),
    CONSTRAINT user_profiles_dietary_is_array CHECK (
        jsonb_typeof(dietary_preferences) = 'array'
    ),
    CONSTRAINT user_profiles_allergies_is_array CHECK (
        jsonb_typeof(allergies_restrictions) = 'array'
    )
);

COMMENT ON TABLE user_profiles IS
    'Domain LifeSight profile (fitness/nutrition/accessibility). '
    'display_name and email remain authoritative on users.';

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS admin_audit_log_created_at_idx
    ON admin_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS admin_audit_log_target_user_id_idx
    ON admin_audit_log (target_user_id);

COMMENT ON TABLE admin_audit_log IS
    'Audit trail for offline seed/admin scripts. No public /admin HTTP in V1.';
