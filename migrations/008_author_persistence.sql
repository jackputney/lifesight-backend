-- 008: Author persistence — projects, documents, immutable version history.
-- Ownership is always public.users (post-007). Do not reference Supabase auth.
-- Child rows CASCADE when a project or document is deleted (explicit parent delete).
-- Document updates are optimistic via author_documents.revision (never rewrite version rows).

CREATE TABLE IF NOT EXISTS author_projects (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT author_projects_title_nonempty CHECK (char_length(btrim(title)) > 0)
);

CREATE INDEX IF NOT EXISTS author_projects_user_created_idx
    ON author_projects (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS author_documents (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID NOT NULL REFERENCES author_projects(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    revision     INTEGER NOT NULL DEFAULT 1,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT author_documents_title_nonempty CHECK (char_length(btrim(title)) > 0),
    CONSTRAINT author_documents_revision_positive CHECK (revision >= 1)
);

CREATE INDEX IF NOT EXISTS author_documents_project_updated_idx
    ON author_documents (project_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS author_documents_user_updated_idx
    ON author_documents (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS author_document_versions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id  UUID NOT NULL REFERENCES author_documents(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revision     INTEGER NOT NULL,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT author_document_versions_revision_positive CHECK (revision >= 1),
    CONSTRAINT author_document_versions_doc_revision_uidx UNIQUE (document_id, revision)
);

CREATE INDEX IF NOT EXISTS author_document_versions_doc_rev_idx
    ON author_document_versions (document_id, revision DESC);

COMMENT ON TABLE author_projects IS
    'Author mode projects. user_id from JWT only; never from request body.';
COMMENT ON TABLE author_documents IS
    'Current document head. revision is the optimistic-concurrency token for autosave.';
COMMENT ON TABLE author_document_versions IS
    'Append-only snapshots. Rows are never updated; history is preserved.';
