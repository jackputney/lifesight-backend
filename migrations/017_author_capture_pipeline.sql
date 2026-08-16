-- 017: Author capture → refine → flag → review pipeline.
--
-- Coexists with 008 (author_projects / author_documents / author_document_versions).
-- It does not replace that surface; nothing here references it.
--
-- Core contract: raw dictation is IMMUTABLE and APPEND-ONLY.
--   author_captures        = "what the user actually said"   (never rewritten)
--   author_draft_versions  = "what LifeSight refined it into" (derivative, versioned)
-- Refinement never edits a capture; it inserts a new draft version derived from a
-- capture-sequence range. Flags are advisory rows that explain a possible problem;
-- accepting or editing one inserts ANOTHER draft version rather than mutating one.
--
-- Ownership is always public.users (post-007), user_id from the JWT only.
-- conversation_id and manuscript_id are deliberately soft references (no FK):
-- a capture session must survive a conversation or manuscript it was linked to.

CREATE TABLE IF NOT EXISTS author_sessions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    conversation_id  UUID,
    manuscript_id    UUID,
    title            TEXT,
    status           TEXT NOT NULL DEFAULT 'active',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    CONSTRAINT author_sessions_status_chk CHECK (status IN ('active', 'ended'))
);

CREATE INDEX IF NOT EXISTS author_sessions_user_created_idx
    ON author_sessions (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS author_captures (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id   UUID NOT NULL REFERENCES author_sessions(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    sequence     INTEGER NOT NULL,
    source       TEXT NOT NULL,
    raw_text     TEXT NOT NULL,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT author_captures_source_chk CHECK (source IN ('voice', 'typed')),
    CONSTRAINT author_captures_raw_text_nonempty CHECK (char_length(btrim(raw_text)) > 0),
    CONSTRAINT author_captures_sequence_nonnegative CHECK (sequence >= 0),
    CONSTRAINT author_captures_session_sequence_uidx UNIQUE (session_id, sequence)
);

CREATE INDEX IF NOT EXISTS author_captures_session_sequence_idx
    ON author_captures (session_id, sequence);

-- Immutability enforced in the DATABASE, not only in the application layer.
-- Any UPDATE or DELETE of a capture row aborts the statement. The application
-- additionally issues no UPDATE/DELETE against this table and exposes no route
-- that could mutate a capture — this trigger is the backstop for that promise.
--
-- Operational consequence, on purpose: because the raise also fires for cascaded
-- deletes, erasing a user or a session that owns captures requires an explicit
-- maintenance transaction that disables this trigger
-- (ALTER TABLE author_captures DISABLE TRIGGER author_captures_no_update_delete).
-- No HTTP route in this PR deletes a user, a session, or a capture.
CREATE OR REPLACE FUNCTION author_captures_reject_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'author_captures is append-only: % on capture % is forbidden. Raw dictation is immutable; refinement inserts author_draft_versions rows instead.',
        TG_OP, OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$;

DROP TRIGGER IF EXISTS author_captures_no_update_delete ON author_captures;
CREATE TRIGGER author_captures_no_update_delete
    BEFORE UPDATE OR DELETE ON author_captures
    FOR EACH ROW EXECUTE FUNCTION author_captures_reject_mutation();

CREATE TABLE IF NOT EXISTS author_draft_versions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              UUID NOT NULL REFERENCES author_sessions(id) ON DELETE CASCADE,
    user_id                 UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    version                 INTEGER NOT NULL,
    refinement_level        TEXT NOT NULL,
    content                 TEXT NOT NULL,
    source_capture_from     INTEGER NOT NULL,
    source_capture_to       INTEGER NOT NULL,
    derived_from_version_id UUID REFERENCES author_draft_versions(id) ON DELETE SET NULL,
    model_identifier        TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT author_draft_versions_level_chk CHECK (
        refinement_level IN ('light_cleanup', 'preserve_voice', 'polish', 'structural_rewrite')
    ),
    CONSTRAINT author_draft_versions_version_positive CHECK (version >= 1),
    CONSTRAINT author_draft_versions_capture_range_chk CHECK (
        source_capture_from >= 0 AND source_capture_to >= source_capture_from
    ),
    CONSTRAINT author_draft_versions_session_version_uidx UNIQUE (session_id, version)
);

CREATE INDEX IF NOT EXISTS author_draft_versions_session_version_idx
    ON author_draft_versions (session_id, version DESC);

CREATE TABLE IF NOT EXISTS author_flags (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        UUID NOT NULL REFERENCES author_sessions(id) ON DELETE CASCADE,
    draft_version_id  UUID NOT NULL REFERENCES author_draft_versions(id) ON DELETE CASCADE,
    user_id           UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    category          TEXT NOT NULL,
    span_start        INTEGER,
    span_end          INTEGER,
    explanation       TEXT NOT NULL,
    suggested_change  TEXT,
    status            TEXT NOT NULL DEFAULT 'open',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT author_flags_category_chk CHECK (
        category IN (
            'typo', 'grammar', 'repetition', 'tangent',
            'unclear', 'contradiction', 'structure', 'other'
        )
    ),
    CONSTRAINT author_flags_status_chk CHECK (
        status IN ('open', 'accepted', 'rejected', 'edited', 'deferred')
    ),
    CONSTRAINT author_flags_explanation_nonempty CHECK (char_length(btrim(explanation)) > 0),
    -- A flag is either fully localized (both offsets) or advisory (neither).
    CONSTRAINT author_flags_span_pairing_chk CHECK (
        (span_start IS NULL AND span_end IS NULL)
        OR (span_start IS NOT NULL AND span_end IS NOT NULL
            AND span_start >= 0 AND span_end >= span_start)
    )
);

CREATE INDEX IF NOT EXISTS author_flags_session_status_idx
    ON author_flags (session_id, status);

CREATE INDEX IF NOT EXISTS author_flags_draft_version_idx
    ON author_flags (draft_version_id);

CREATE TABLE IF NOT EXISTS author_flag_decisions (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flag_id                   UUID NOT NULL REFERENCES author_flags(id) ON DELETE CASCADE,
    user_id                   UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    decision                  TEXT NOT NULL,
    replacement_text          TEXT,
    resulting_draft_version_id UUID REFERENCES author_draft_versions(id) ON DELETE SET NULL,
    decided_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT author_flag_decisions_decision_chk CHECK (
        decision IN ('accept', 'reject', 'edit', 'defer')
    ),
    CONSTRAINT author_flag_decisions_edit_requires_text_chk CHECK (
        decision <> 'edit' OR replacement_text IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS author_flag_decisions_flag_idx
    ON author_flag_decisions (flag_id, decided_at DESC);

COMMENT ON TABLE author_sessions IS
    'One dictation/capture session. Owns immutable captures and the derived draft versions. conversation_id / manuscript_id are soft references (no FK) on purpose.';
COMMENT ON TABLE author_captures IS
    'Raw dictation exactly as the user said it. APPEND-ONLY and IMMUTABLE: the author_captures_no_update_delete trigger raises on UPDATE or DELETE. Refinement never rewrites a capture.';
COMMENT ON TABLE author_draft_versions IS
    'Derivative refined text. Each row is a new immutable version built from the capture-sequence range [source_capture_from, source_capture_to]; prior versions are never rewritten. model_identifier is NULL for human-applied edits.';
COMMENT ON TABLE author_flags IS
    'Advisory review notes on ONE draft version. A flag explains a possible problem and never silently changes the writing. span_start/span_end are character offsets into that version content, or both NULL when the note is not localizable.';
COMMENT ON TABLE author_flag_decisions IS
    'Audit of the author resolving a flag. accept/edit insert a NEW author_draft_versions row (resulting_draft_version_id); reject/defer change no text. Nothing here can touch author_captures.';
