-- PROPOSED — DO NOT APPLY until reviewed.
-- Path: docs/proposed/ (NOT migrations/) so run_migrations.py will not pick it up.
--
-- Describes intended schema after app commit 613e6e3.
-- Companion contract: docs/V2_IOS_CONTRACT_NOTE.md
--
-- Problem: personal_records.exercise_id / set_logs.exercise_id FK to
-- planned_exercises(id). Re-upload/regenerate plan → new IDs → PR/history break.
--
-- Direction: stable exercises catalog; plan/log/PR rows reference that.

-- =====================================================================
-- Backfill / identity rules (review these before applying anything)
-- =====================================================================
--
-- Scope of catalog IDs
--   * System-wide rows: exercises.user_id IS NULL (shared canonical names).
--   * User-private / custom: exercises.user_id = owner (never merge across users).
--
-- Canonicalization (automatic, conservative)
--   * Trim, collapse whitespace, casefold for matching only.
--   * Do NOT strip equipment words automatically ("Barbell Bench Press" ≠
--     "Bench Press") — those are aliases or manual merges, not auto-collapses.
--
-- Aliases
--   * exercise_aliases holds alternate strings ("BB Bench", "Flat Barbell Bench").
--   * Matching order: exact canonical → alias → leave unmatched.
--
-- Ambiguous matches
--   * If multiple catalog rows score equally, DO NOT auto-merge.
--   * Leave catalog_exercise_id NULL and queue for manual review.
--
-- Unmatched / custom
--   * Create a user-private exercises row with canonical_name = source name.
--   * Keep logging/PRs usable immediately; promote to system canonical later
--     only via explicit admin/user action.
--
-- Deletes
--   * Once referenced by set_logs or personal_records, catalog delete is
--     prohibited (RESTRICT) or soft-delete only — never hard-delete in place.
--
-- Cutover uniqueness (after backfill)
--   UNIQUE (user_id, catalog_exercise_id, rep_range) on personal_records.

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
ALTER TABLE planned_exercises
    ADD COLUMN IF NOT EXISTS catalog_exercise_id UUID REFERENCES exercises(id);

ALTER TABLE set_logs
    ADD COLUMN IF NOT EXISTS catalog_exercise_id UUID REFERENCES exercises(id);

ALTER TABLE personal_records
    ADD COLUMN IF NOT EXISTS catalog_exercise_id UUID REFERENCES exercises(id);

-- Backfill procedure sketch:
--   1) For each distinct (user_id, planned_exercises.name), try alias/canonical
--      match; else create user-private exercises row.
--   2) SET planned_exercises.catalog_exercise_id from that map.
--   3) Propagate to set_logs / personal_records via planned_exercises or
--      conservative name match; leave NULL if ambiguous.
--   4) Manual review queue for NULL catalog_exercise_id with volume > 0.
--   5) After code cutover, drop FKs to planned_exercises.id and rename
--      catalog_exercise_id → exercise_id; enforce PR UNIQUE above.

-- =====================================================================
-- health_metrics.unit — recommend BEFORE production Terra ingestion
-- (separate small migration is fine; listed here so it is not forgotten)
-- =====================================================================
-- ALTER TABLE health_metrics ADD COLUMN IF NOT EXISTS unit TEXT;
-- Store unit at write time (kg/lb, m/mi, s/min, bpm, mg/dL, …).
-- Do not wait until UI work; ambiguous stored values are hard to repair.
