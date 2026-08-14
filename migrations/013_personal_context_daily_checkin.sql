-- 013: personal-context profile fields + daily check-ins + checkin mode.
-- Additive only. Existing user_profiles rows remain valid (nullable columns).

ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS occupation TEXT,
    ADD COLUMN IF NOT EXISTS industry TEXT,
    ADD COLUMN IF NOT EXISTS education_context TEXT,
    ADD COLUMN IF NOT EXISTS interests JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS typical_schedule TEXT;

COMMENT ON COLUMN user_profiles.occupation IS
    'Optional work/role label for personal context (not daily state).';
COMMENT ON COLUMN user_profiles.industry IS
    'Optional industry/field for personal context.';
COMMENT ON COLUMN user_profiles.education_context IS
    'Optional school/education context.';
COMMENT ON COLUMN user_profiles.interests IS
    'JSON string array of interests (max 20 / 120 chars enforced in API).';
COMMENT ON COLUMN user_profiles.typical_schedule IS
    'Optional free-text typical schedule (e.g. work 9–5 weekdays).';

CREATE TABLE IF NOT EXISTS daily_checkins (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    local_date        DATE NOT NULL,
    timezone          TEXT NOT NULL DEFAULT 'UTC',
    conversation_id   UUID REFERENCES conversations(id) ON DELETE SET NULL,
    status            TEXT NOT NULL DEFAULT 'not_started',
    sleep_hours       DOUBLE PRECISION,
    sleep_quality     INTEGER,
    energy            INTEGER,
    mood              INTEGER,
    stress            INTEGER,
    soreness          INTEGER,
    top_priority      TEXT,
    notes             TEXT,
    summary           TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT daily_checkins_status_chk CHECK (
        status IN ('not_started', 'in_progress', 'completed')
    ),
    CONSTRAINT daily_checkins_sleep_quality_chk CHECK (
        sleep_quality IS NULL OR (sleep_quality BETWEEN 1 AND 5)
    ),
    CONSTRAINT daily_checkins_energy_chk CHECK (
        energy IS NULL OR (energy BETWEEN 1 AND 5)
    ),
    CONSTRAINT daily_checkins_mood_chk CHECK (
        mood IS NULL OR (mood BETWEEN 1 AND 5)
    ),
    CONSTRAINT daily_checkins_stress_chk CHECK (
        stress IS NULL OR (stress BETWEEN 1 AND 5)
    ),
    CONSTRAINT daily_checkins_soreness_chk CHECK (
        soreness IS NULL OR (soreness BETWEEN 1 AND 5)
    ),
    CONSTRAINT daily_checkins_sleep_hours_chk CHECK (
        sleep_hours IS NULL OR (sleep_hours >= 0 AND sleep_hours <= 24)
    ),
    CONSTRAINT daily_checkins_user_local_date_uidx UNIQUE (user_id, local_date)
);

CREATE INDEX IF NOT EXISTS daily_checkins_user_date_idx
    ON daily_checkins (user_id, local_date DESC);

COMMENT ON TABLE daily_checkins IS
    'Dated daily state (sleep/energy/mood/…). Not permanent /profile identity.';

-- Allow dedicated checkin conversational mode (hidden from GET /modes).
ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_mode_check;
ALTER TABLE conversations ADD CONSTRAINT conversations_mode_check
    CHECK (mode IN (
        'fitness', 'diet', 'author', 'brainstorm', 'mail_calendar', 'jarvis', 'checkin'
    ));

ALTER TABLE action_log DROP CONSTRAINT IF EXISTS action_log_mode_check;
ALTER TABLE action_log ADD CONSTRAINT action_log_mode_check
    CHECK (mode IN (
        'fitness', 'diet', 'author', 'brainstorm', 'mail_calendar', 'jarvis', 'checkin'
    ));

ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS pending_actions_source_mode_check;
ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_source_mode_check
    CHECK (source_mode IN (
        'fitness', 'diet', 'author', 'brainstorm', 'mail_calendar', 'jarvis', 'checkin'
    ));
