-- 010_profiles.sql
-- The app-owned facts about a person, on top of identity. 006/007 moved
-- identity to public.users (self-hosted); this FKs there, not auth.users.
-- Requires 006 and 007 to have already run.
--
-- Nothing before this migration recorded a name, an age, or a preference —
-- users (006) holds login identity, and 001/002/003/004 are entirely
-- activity rows (messages, sets, food, chapters). This does not reopen the
-- identity decision; profiles hangs 1:1 off users and holds only what the
-- app needs and an operator can edit.
--
-- NOTE for review: users.display_name (006) already exists. This adds
-- another display_name on profiles, which may be redundant — left as-is
-- pending review rather than deciding unilaterally which one is canonical.
--
-- Fitness and diet need age / sex / height / weight for anything real (BMR,
-- target heart rate, calorie targets), so this unblocks them too, not just
-- the admin panel.

-- profiles — one row per user, created lazily on first write ----------------
CREATE TABLE IF NOT EXISTS profiles (
    user_id       UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,

    -- Identity / addressing. display_name is what Olivia calls the user aloud.
    display_name  TEXT,
    full_name     TEXT,
    pronouns      TEXT,

    -- Stored as a date, never as an integer age — an age column is wrong the
    -- day after it is written. Age is derived at read time.
    date_of_birth DATE,

    -- Body metrics for fitness/diet. SI units at rest (the v2 contract does
    -- unit conversion at the API edge, so storage stays unambiguous).
    sex_at_birth  TEXT CHECK (sex_at_birth IN ('female', 'male', 'intersex', 'undisclosed')),
    height_cm     NUMERIC(5,1) CHECK (height_cm > 0 AND height_cm < 300),
    weight_kg     NUMERIC(5,1) CHECK (weight_kg > 0 AND weight_kg < 700),

    -- Accessibility. The primary user is near-blind; these are read by the
    -- client and by spoken-readback pacing, not cosmetic.
    speech_rate   NUMERIC(3,2) DEFAULT 1.0 CHECK (speech_rate BETWEEN 0.5 AND 2.0),
    voice_id      TEXT,                     -- ElevenLabs voice override, per user
    timezone      TEXT NOT NULL DEFAULT 'UTC',
    locale        TEXT NOT NULL DEFAULT 'en-US',

    -- Free-form operator notes. Deliberately not structured: this is where a
    -- caregiver writes context that has no column yet.
    notes         TEXT,

    -- Operator bookkeeping.
    is_primary    BOOLEAN NOT NULL DEFAULT FALSE,  -- the near-blind primary user
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'suspended', 'test')),

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profiles_status ON profiles (status);

-- At most one primary user, enforced in the schema rather than in app code.
CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_single_primary
    ON profiles (is_primary) WHERE is_primary;

-- updated_at maintenance ----------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_profiles_updated_at ON profiles;
CREATE TRIGGER trg_profiles_updated_at
    BEFORE UPDATE ON profiles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Age is derived, never stored. Null-safe: no DOB → no age.
CREATE OR REPLACE VIEW profile_overview AS
SELECT
    p.user_id,
    p.display_name,
    p.full_name,
    p.status,
    p.is_primary,
    p.date_of_birth,
    CASE WHEN p.date_of_birth IS NULL THEN NULL
         ELSE EXTRACT(YEAR FROM age(p.date_of_birth))::INT
    END AS age_years,
    p.timezone,
    p.updated_at
FROM profiles p;
