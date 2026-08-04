-- PROPOSED — DO NOT APPLY until reviewed.
-- Not under migrations/*.sql so scripts/run_migrations.py will not pick it up.
--
-- Problem: personal_records.exercise_id / set_logs.exercise_id currently FK to
-- planned_exercises(id). Re-uploading or regenerating a plan creates new
-- planned_exercises rows, so "Bench Press" gets a new identity and PRs /
-- history fragment.
--
-- Direction: stable exercises catalog; plan rows and logs reference that.

-- Stable catalog -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS exercises (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    -- NULL user_id = system-wide canonical exercise; non-null = user-private
    canonical_name TEXT NOT NULL,
    equipment      TEXT,
    muscle_group   TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exercises_user_canonical
    ON exercises (user_id, lower(canonical_name));
CREATE INDEX IF NOT EXISTS idx_exercises_canonical
    ON exercises (lower(canonical_name));

CREATE TABLE IF NOT EXISTS exercise_aliases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    exercise_id UUID NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    UNIQUE (exercise_id, lower(alias))
);
CREATE INDEX IF NOT EXISTS idx_exercise_aliases_alias
    ON exercise_aliases (lower(alias));

-- planned_exercises: keep prescription fields; point at stable exercise ----
-- Step A (additive, non-breaking): add nullable catalog FK
ALTER TABLE planned_exercises
    ADD COLUMN IF NOT EXISTS catalog_exercise_id UUID REFERENCES exercises(id);

-- set_logs / personal_records: add catalog FK alongside legacy column ------
ALTER TABLE set_logs
    ADD COLUMN IF NOT EXISTS catalog_exercise_id UUID REFERENCES exercises(id);

ALTER TABLE personal_records
    ADD COLUMN IF NOT EXISTS catalog_exercise_id UUID REFERENCES exercises(id);

-- Backfill sketch (run after deploying code that writes catalog ids):
--   1) For each distinct planned_exercises.name per user, upsert exercises.
--   2) SET planned_exercises.catalog_exercise_id from that map.
--   3) SET set_logs.catalog_exercise_id / personal_records.catalog_exercise_id
--      from their planned_exercises row (or name match).
--   4) After backfill + code cutover, drop old exercise_id FKs to
--      planned_exercises and rename catalog_exercise_id → exercise_id.
--
-- Do NOT collapse PR uniqueness onto planned_exercises.id.
-- Target UNIQUE for PRs after cutover:
--   UNIQUE (user_id, catalog_exercise_id, rep_range)

-- Optional later: unit on health_metrics (separate small migration)
-- ALTER TABLE health_metrics ADD COLUMN IF NOT EXISTS unit TEXT;
