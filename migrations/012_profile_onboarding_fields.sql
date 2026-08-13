-- 012: additive onboarding fields on user_profiles (nullable; existing rows OK).
-- Does not add CHECK constraints on legacy free-text training_frequency / primary_goals
-- membership so older stored values continue to decode.

ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS preferred_units TEXT,
    ADD COLUMN IF NOT EXISTS training_environment TEXT,
    ADD COLUMN IF NOT EXISTS typical_session_minutes INTEGER,
    ADD COLUMN IF NOT EXISTS sex_for_physiological_calculations TEXT;

ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_preferred_units_chk;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_preferred_units_chk CHECK (
        preferred_units IS NULL
        OR preferred_units IN ('imperial', 'metric')
    );

ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_training_environment_chk;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_training_environment_chk CHECK (
        training_environment IS NULL
        OR training_environment IN (
            'commercial_gym',
            'home_gym',
            'limited_equipment',
            'bodyweight_outdoors',
            'mixed'
        )
    );

ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_typical_session_minutes_chk;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_typical_session_minutes_chk CHECK (
        typical_session_minutes IS NULL
        OR (
            typical_session_minutes >= 10
            AND typical_session_minutes <= 300
        )
    );

ALTER TABLE user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_sex_physio_chk;
ALTER TABLE user_profiles
    ADD CONSTRAINT user_profiles_sex_physio_chk CHECK (
        sex_for_physiological_calculations IS NULL
        OR sex_for_physiological_calculations IN (
            'male',
            'female',
            'unspecified'
        )
    );

COMMENT ON COLUMN user_profiles.preferred_units IS
    'Measurement units: imperial | metric. Nullable for existing users.';
COMMENT ON COLUMN user_profiles.training_environment IS
    'Where the user typically trains (commercial_gym, home_gym, …).';
COMMENT ON COLUMN user_profiles.typical_session_minutes IS
    'Typical workout duration in minutes (10–300).';
COMMENT ON COLUMN user_profiles.sex_for_physiological_calculations IS
    'Optional male|female|unspecified for formula/reference use only — not gender identity.';
COMMENT ON COLUMN user_profiles.primary_goals IS
    'Ordered goals JSON array: index 0 = primary, 1–2 optional secondary; max 3 in V1 API.';
COMMENT ON COLUMN user_profiles.training_frequency IS
    'Canonical V1 wire: 0_1 | 2 | 3 | 4 | 5 | 6_plus (legacy free-text may still exist).';
