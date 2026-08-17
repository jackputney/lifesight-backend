-- 019: Fitness workout V1 completion — additive on 003 workout_* tables.
--
-- Does NOT invent a second workout model. Strengthens ownership on nested
-- rows, adds active-plan semantics, exercise notes, and one-active-session.
-- Historical plans remain when a new plan is activated.
--
-- From zero: 003 creates the tables; this migration adds columns/FKs.
-- From current main: same ALTERs; backfill copies user_id from parents
-- (no-op on empty tables).

-- ---------------------------------------------------------------------------
-- Plan metadata + active-plan (historical rows stay)
-- ---------------------------------------------------------------------------
ALTER TABLE workout_plans
    ADD COLUMN IF NOT EXISTS title TEXT,
    ADD COLUMN IF NOT EXISTS notes TEXT,
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

COMMENT ON COLUMN workout_plans.is_active IS
    'At most one active plan per user. Activating another plan sets this '
    'false — rows are never deleted by activation.';
COMMENT ON COLUMN workout_plans.title IS
    'Optional display title. NULL on pre-019 rows.';
COMMENT ON COLUMN workout_plans.notes IS
    'Optional plan-level notes. Not a system prompt.';

CREATE UNIQUE INDEX IF NOT EXISTS workout_plans_one_active_per_user
    ON workout_plans (user_id)
    WHERE is_active;

ALTER TABLE planned_exercises
    ADD COLUMN IF NOT EXISTS notes TEXT;

COMMENT ON COLUMN planned_exercises.notes IS
    'Optional per-exercise coaching note (also used by visual_panel.notes).';

-- ---------------------------------------------------------------------------
-- Owner columns on nested tables + composite FKs (no cross-user children)
-- ---------------------------------------------------------------------------
ALTER TABLE workout_days
    ADD COLUMN IF NOT EXISTS user_id UUID;
ALTER TABLE planned_exercises
    ADD COLUMN IF NOT EXISTS user_id UUID;
ALTER TABLE set_logs
    ADD COLUMN IF NOT EXISTS user_id UUID;

UPDATE workout_days d
SET user_id = p.user_id
FROM workout_plans p
WHERE d.plan_id = p.id
  AND d.user_id IS NULL;

UPDATE planned_exercises e
SET user_id = d.user_id
FROM workout_days d
WHERE e.day_id = d.id
  AND e.user_id IS NULL;

UPDATE set_logs s
SET user_id = ws.user_id
FROM workout_sessions ws
WHERE s.session_id = ws.id
  AND s.user_id IS NULL;

-- Orphan nested rows cannot exist after 003 FKs; SET NOT NULL is safe.
ALTER TABLE workout_days
    ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE planned_exercises
    ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE set_logs
    ALTER COLUMN user_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'workout_plans_id_user_uidx'
    ) THEN
        ALTER TABLE workout_plans
            ADD CONSTRAINT workout_plans_id_user_uidx UNIQUE (id, user_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'workout_days_id_user_uidx'
    ) THEN
        ALTER TABLE workout_days
            ADD CONSTRAINT workout_days_id_user_uidx UNIQUE (id, user_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'planned_exercises_id_user_uidx'
    ) THEN
        ALTER TABLE planned_exercises
            ADD CONSTRAINT planned_exercises_id_user_uidx UNIQUE (id, user_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'workout_sessions_id_user_uidx'
    ) THEN
        ALTER TABLE workout_sessions
            ADD CONSTRAINT workout_sessions_id_user_uidx UNIQUE (id, user_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'workout_days_user_id_fkey'
    ) THEN
        ALTER TABLE workout_days
            ADD CONSTRAINT workout_days_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'workout_days_plan_user_fkey'
    ) THEN
        ALTER TABLE workout_days
            ADD CONSTRAINT workout_days_plan_user_fkey
            FOREIGN KEY (plan_id, user_id)
            REFERENCES workout_plans (id, user_id) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'planned_exercises_user_id_fkey'
    ) THEN
        ALTER TABLE planned_exercises
            ADD CONSTRAINT planned_exercises_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'planned_exercises_day_user_fkey'
    ) THEN
        ALTER TABLE planned_exercises
            ADD CONSTRAINT planned_exercises_day_user_fkey
            FOREIGN KEY (day_id, user_id)
            REFERENCES workout_days (id, user_id) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'set_logs_user_id_fkey'
    ) THEN
        ALTER TABLE set_logs
            ADD CONSTRAINT set_logs_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'set_logs_session_user_fkey'
    ) THEN
        ALTER TABLE set_logs
            ADD CONSTRAINT set_logs_session_user_fkey
            FOREIGN KEY (session_id, user_id)
            REFERENCES workout_sessions (id, user_id) ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'set_logs_exercise_user_fkey'
    ) THEN
        ALTER TABLE set_logs
            ADD CONSTRAINT set_logs_exercise_user_fkey
            FOREIGN KEY (exercise_id, user_id)
            REFERENCES planned_exercises (id, user_id) ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'personal_records_exercise_user_fkey'
    ) THEN
        ALTER TABLE personal_records
            ADD CONSTRAINT personal_records_exercise_user_fkey
            FOREIGN KEY (exercise_id, user_id)
            REFERENCES planned_exercises (id, user_id) ON DELETE RESTRICT;
    END IF;
END;
$$;

-- Dedupe before unique indexes (safe on empty dev DBs; required on legacy 003 data).
WITH ranked_active_sessions AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY user_id ORDER BY started_at DESC, id DESC
           ) AS rn
    FROM workout_sessions
    WHERE status = 'active'
)
UPDATE workout_sessions ws
SET status = 'abandoned',
    ended_at = COALESCE(ws.ended_at, now())
FROM ranked_active_sessions r
WHERE ws.id = r.id
  AND r.rn > 1;

WITH ranked_duplicate_sets AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY session_id, exercise_id, set_number
               ORDER BY completed_at DESC NULLS LAST, id DESC
           ) AS rn
    FROM set_logs
)
DELETE FROM set_logs sl
USING ranked_duplicate_sets r
WHERE sl.id = r.id
  AND r.rn > 1;

-- At most one active workout session per user (003 index was not unique).
CREATE UNIQUE INDEX IF NOT EXISTS workout_sessions_one_active_per_user
    ON workout_sessions (user_id)
    WHERE status = 'active';

-- One logged set number per exercise per session.
CREATE UNIQUE INDEX IF NOT EXISTS set_logs_session_exercise_set_uidx
    ON set_logs (session_id, exercise_id, set_number);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'set_logs_set_number_chk'
    ) THEN
        ALTER TABLE set_logs
            ADD CONSTRAINT set_logs_set_number_chk
            CHECK (set_number >= 1 AND set_number <= 30);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'set_logs_reps_chk'
    ) THEN
        ALTER TABLE set_logs
            ADD CONSTRAINT set_logs_reps_chk
            CHECK (reps IS NULL OR (reps >= 0 AND reps <= 500));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'set_logs_weight_chk'
    ) THEN
        ALTER TABLE set_logs
            ADD CONSTRAINT set_logs_weight_chk
            CHECK (weight IS NULL OR (weight >= 0 AND weight <= 2000));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'personal_records_rep_range_chk'
    ) THEN
        ALTER TABLE personal_records
            ADD CONSTRAINT personal_records_rep_range_chk
            CHECK (rep_range >= 1 AND rep_range <= 500);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'personal_records_weight_chk'
    ) THEN
        ALTER TABLE personal_records
            ADD CONSTRAINT personal_records_weight_chk
            CHECK (weight >= 0 AND weight <= 2000);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'planned_exercises_target_sets_chk'
    ) THEN
        ALTER TABLE planned_exercises
            ADD CONSTRAINT planned_exercises_target_sets_chk
            CHECK (target_sets IS NULL OR (target_sets >= 1 AND target_sets <= 30));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'planned_exercises_target_reps_chk'
    ) THEN
        ALTER TABLE planned_exercises
            ADD CONSTRAINT planned_exercises_target_reps_chk
            CHECK (target_reps IS NULL OR (target_reps >= 1 AND target_reps <= 500));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'planned_exercises_rest_chk'
    ) THEN
        ALTER TABLE planned_exercises
            ADD CONSTRAINT planned_exercises_rest_chk
            CHECK (rest_seconds IS NULL OR (rest_seconds >= 0 AND rest_seconds <= 3600));
    END IF;
END;
$$;

COMMENT ON TABLE workout_plans IS
    'User-owned training plans. is_active marks the current plan; older rows '
    'remain as history. Nested days/exercises carry user_id so children cannot '
    'attach to another user''s plan.';
COMMENT ON TABLE workout_sessions IS
    'A started workout. At most one status=active row per user. Completing or '
    'abandoning is explicit — start resumes an existing active session.';
COMMENT ON TABLE personal_records IS
    'Best logged weight per (user, planned_exercise, rep_range). A 5-rep best '
    'never overwrites a 1-rep best. Weight is unitless; see FITNESS_WORKOUT_V1 '
    'contract. exercise_id still references planned_exercises (unstable across '
    'plan rebuilds) — a stable catalog is intentionally not in this slice.';
