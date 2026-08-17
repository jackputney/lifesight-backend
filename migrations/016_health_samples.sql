-- 016: normalized health samples for BOTH providers (HealthKit + Terra).
--
-- Supersedes the aggregate-shaped health_metrics (003): that table has a single
-- recorded_at, no external sample id, no unit, no provider discriminator, and
-- no dedupe key, so a replayed webhook or a re-synced device duplicates rows.
-- Nothing in the codebase reads health_metrics, so this is a writer migration
-- only: the Terra webhook now writes health_samples and stops writing
-- health_metrics. health_metrics is left in place (no data loss, no DROP) and
-- marked deprecated below.
--
-- sample_type is a closed allowlist on purpose (the opposite of
-- health_metrics.metric_type free text): the AI health-context tool aggregates
-- per type, and an unbounded type space makes those aggregates meaningless.
-- Unmappable Terra metrics are dropped at ingest and counted, never invented.

CREATE TABLE IF NOT EXISTS health_samples (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    sample_type   TEXT NOT NULL,
    start_at      TIMESTAMPTZ NOT NULL,
    end_at        TIMESTAMPTZ NOT NULL,
    value         DOUBLE PRECISION,
    unit          TEXT,
    value_text    TEXT,
    source_bundle TEXT,
    source_name   TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT health_samples_provider_chk CHECK (
        provider IN ('healthkit', 'terra')
    ),
    CONSTRAINT health_samples_sample_type_chk CHECK (
        sample_type IN (
            'steps',
            'heart_rate',
            'resting_heart_rate',
            'sleep',
            'workout',
            'active_energy',
            'distance_walking_running',
            'body_mass'
        )
    ),
    CONSTRAINT health_samples_interval_chk CHECK (end_at >= start_at),
    -- Numeric types carry a canonical unit; only the categorical types
    -- ('sleep', 'workout') may store a bare value_text with no unit.
    CONSTRAINT health_samples_unit_required_chk CHECK (
        sample_type IN ('sleep', 'workout') OR unit IS NOT NULL
    ),
    CONSTRAINT health_samples_value_present_chk CHECK (
        value IS NOT NULL OR value_text IS NOT NULL
    ),
    CONSTRAINT health_samples_external_id_nonempty_chk CHECK (
        char_length(btrim(external_id)) > 0
    ),
    -- Dedupe key: idempotent upsert target for device re-sync and webhook replay.
    CONSTRAINT health_samples_provider_external_uidx UNIQUE (user_id, provider, external_id)
);

CREATE INDEX IF NOT EXISTS health_samples_user_type_start_idx
    ON health_samples (user_id, sample_type, start_at DESC);

COMMENT ON TABLE health_samples IS
    'Normalized per-sample health data from HealthKit and Terra. '
    'UNIQUE (user_id, provider, external_id) makes ingest idempotent: HealthKit '
    'sends its sample UUID, Terra gets a deterministic synthetic id. Reads are '
    'always scoped by user_id; aggregates only are exposed to the model.';
COMMENT ON COLUMN health_samples.provider IS
    'Ingest source: healthkit (POST /healthkit/sync) or terra (webhook).';
COMMENT ON COLUMN health_samples.external_id IS
    'HealthKit sample UUID, or sha256(metric_type|recorded_at|source|value) for Terra.';
COMMENT ON COLUMN health_samples.value_text IS
    'Categorical payload (e.g. sleep stage, workout activity) when value is not numeric.';
COMMENT ON COLUMN health_samples.unit IS
    'Canonical unit for the type (count, count/min, kcal, m, kg, min). '
    'Client units are converted at ingest, never stored as sent.';

-- Truthful "last time this user's device completed a sync", which cannot be
-- derived from health_samples: a sync that uploads only already-known samples
-- writes no rows but still happened.
CREATE TABLE IF NOT EXISTS health_sync_state (
    user_id        UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE health_sync_state IS
    'One row per user: when POST /healthkit/sync last completed. Per-category '
    'freshness is derived from health_samples aggregates instead.';

COMMENT ON TABLE health_metrics IS
    'DEPRECATED (016) — superseded by health_samples. Aggregate-shaped, no '
    'external sample id, no unit, no provider column, no dedupe key. Retained '
    'for historical Terra rows only; no code reads or writes it.';
