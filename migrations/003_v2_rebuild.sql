-- 003_v2_rebuild.sql
-- LifeSight v2 data model: fitness, diet, Postgres-native author, wearables.
-- Does NOT edit 001/002 — those remain schema history (including Google-Docs
-- writing_* tables, which are superseded and must not receive new app writes).
--
-- Mode registry (v2): fitness / diet / author. jarvis stays in CHECK lists so
-- existing rows and inert registry code keep working; health remains allowed
-- for historical conversation/pending_action rows only.
--
-- Column note: SQL reserved word ORDER is mapped to sort_order everywhere the
-- v2 build spec said "order".

-- Widen mode CHECKs on shared tables (keep old values for history / jarvis) --
ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_mode_check;
ALTER TABLE conversations ADD CONSTRAINT conversations_mode_check
    CHECK (mode IN ('author', 'health', 'jarvis', 'fitness', 'diet'));

ALTER TABLE action_log DROP CONSTRAINT IF EXISTS action_log_mode_check;
ALTER TABLE action_log ADD CONSTRAINT action_log_mode_check
    CHECK (mode IN ('author', 'health', 'jarvis', 'fitness', 'diet'));

ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS pending_actions_source_mode_check;
ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_source_mode_check
    CHECK (source_mode IN ('author', 'health', 'jarvis', 'fitness', 'diet'));

-- Fitness -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workout_plans (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    source_upload_ref TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_workout_plans_user ON workout_plans (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS workout_days (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id    UUID NOT NULL REFERENCES workout_plans(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL,
    title      TEXT
);
CREATE INDEX IF NOT EXISTS idx_workout_days_plan ON workout_days (plan_id, sort_order);

CREATE TABLE IF NOT EXISTS planned_exercises (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    day_id        UUID NOT NULL REFERENCES workout_days(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    target_sets   INTEGER,
    target_reps   INTEGER,
    rest_seconds  INTEGER,
    sort_order    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_planned_exercises_day ON planned_exercises (day_id, sort_order);

CREATE TABLE IF NOT EXISTS workout_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    session_date DATE NOT NULL DEFAULT (CURRENT_DATE),
    plan_day_id UUID REFERENCES workout_days(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'completed', 'abandoned')),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_workout_sessions_user ON workout_sessions (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_workout_sessions_active ON workout_sessions (user_id, status)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS set_logs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id   UUID NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
    exercise_id  UUID NOT NULL REFERENCES planned_exercises(id) ON DELETE RESTRICT,
    set_number   INTEGER NOT NULL,
    reps         INTEGER,
    weight       DOUBLE PRECISION,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT NOT NULL DEFAULT 'voice'
                 CHECK (source IN ('voice', 'manual'))
);
CREATE INDEX IF NOT EXISTS idx_set_logs_session ON set_logs (session_id, completed_at);

-- PR is per-exercise-per-rep-range (best 5-rep ≠ best 1-rep).
-- UNIQUE enables upsert when a new best lands for the same range.
CREATE TABLE IF NOT EXISTS personal_records (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    exercise_id  UUID NOT NULL REFERENCES planned_exercises(id) ON DELETE RESTRICT,
    rep_range    INTEGER NOT NULL,
    weight       DOUBLE PRECISION NOT NULL,
    achieved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, exercise_id, rep_range)
);
CREATE INDEX IF NOT EXISTS idx_personal_records_user ON personal_records (user_id, exercise_id);

-- Diet ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS food_entries (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    logged_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    method            TEXT NOT NULL
                      CHECK (method IN ('photo', 'barcode', 'voice', 'manual')),
    raw_input_ref     TEXT,
    matched_food_name TEXT,
    calories          DOUBLE PRECISION,
    protein_g         DOUBLE PRECISION,
    carbs_g           DOUBLE PRECISION,
    fat_g             DOUBLE PRECISION,
    confidence        DOUBLE PRECISION,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_food_entries_user ON food_entries (user_id, logged_at DESC);

CREATE TABLE IF NOT EXISTS daily_nutrition_targets (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    calories          DOUBLE PRECISION,
    protein_g         DOUBLE PRECISION,
    carbs_g           DOUBLE PRECISION,
    fat_g             DOUBLE PRECISION,
    source_upload_ref TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_daily_nutrition_targets_user
    ON daily_nutrition_targets (user_id, created_at DESC);

-- Author (Postgres-native; supersedes writing_documents / sessions / drafts) -
CREATE TABLE IF NOT EXISTS manuscripts (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_manuscripts_user ON manuscripts (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chapters (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    manuscript_id UUID NOT NULL REFERENCES manuscripts(id) ON DELETE CASCADE,
    sort_order    INTEGER NOT NULL,
    title         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chapters_manuscript ON chapters (manuscript_id, sort_order);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chapters_manuscript_order
    ON chapters (manuscript_id, sort_order);

CREATE TABLE IF NOT EXISTS scenes (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chapter_id UUID NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scenes_chapter ON scenes (chapter_id, sort_order);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scenes_chapter_order
    ON scenes (chapter_id, sort_order);

CREATE TABLE IF NOT EXISTS brainstorm_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    manuscript_id   UUID NOT NULL REFERENCES manuscripts(id) ON DELETE CASCADE,
    transcript      TEXT NOT NULL DEFAULT '',
    linked_scene_id UUID REFERENCES scenes(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_brainstorm_sessions_manuscript
    ON brainstorm_sessions (manuscript_id, created_at DESC);

-- Wearables (Terra aggregator) ----------------------------------------------
CREATE TABLE IF NOT EXISTS wearable_connections (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    provider             TEXT NOT NULL,          -- terra provider slug, e.g. 'oura', 'apple'
    aggregator_token_ref TEXT,                   -- encrypted/ref to Terra user token — never plaintext
    connected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_wearable_connections_user ON wearable_connections (user_id);

-- metric_type is free TEXT on purpose — new Terra metrics must not require a migration.
CREATE TABLE IF NOT EXISTS health_metrics (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    metric_type   TEXT NOT NULL,
    value         DOUBLE PRECISION,
    value_json    JSONB,                         -- for complex payloads (sleep stages, etc.)
    source_device  TEXT,
    recorded_at   TIMESTAMPTZ NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_health_metrics_user_type_time
    ON health_metrics (user_id, metric_type, recorded_at DESC);
