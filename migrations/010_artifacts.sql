-- 010: Shared artifact persistence (generic layer for future modes).
-- Append-only. Does not migrate or replace Author tables (008).
-- Ownership is always public.users; never accept user_id from clients.

CREATE TABLE IF NOT EXISTS artifacts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type         TEXT NOT NULL,
    title        TEXT NOT NULL,
    content      JSONB NOT NULL DEFAULT '{}'::jsonb,
    revision     INTEGER NOT NULL DEFAULT 1,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT artifacts_type_nonempty CHECK (char_length(btrim(type)) > 0),
    CONSTRAINT artifacts_title_nonempty CHECK (char_length(btrim(title)) > 0),
    CONSTRAINT artifacts_revision_positive CHECK (revision >= 1)
);

CREATE INDEX IF NOT EXISTS artifacts_user_updated_idx
    ON artifacts (user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS artifacts_user_type_updated_idx
    ON artifacts (user_id, type, updated_at DESC);

CREATE TABLE IF NOT EXISTS artifact_versions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id  UUID NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revision     INTEGER NOT NULL,
    title        TEXT NOT NULL,
    content      JSONB NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT artifact_versions_title_nonempty CHECK (char_length(btrim(title)) > 0),
    CONSTRAINT artifact_versions_revision_positive CHECK (revision >= 1),
    CONSTRAINT artifact_versions_artifact_revision_uidx UNIQUE (artifact_id, revision)
);

CREATE INDEX IF NOT EXISTS artifact_versions_artifact_rev_idx
    ON artifact_versions (artifact_id, revision DESC);

COMMENT ON TABLE artifacts IS
    'Generic mode-agnostic artifact head. user_id from JWT only; content is JSONB.';
COMMENT ON TABLE artifact_versions IS
    'Append-only snapshots of artifacts. Rows are never updated; history is preserved.';
