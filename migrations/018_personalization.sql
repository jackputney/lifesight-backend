-- 018: Adaptive personalization foundation — hierarchical conversation
-- summaries + human-reviewed prompt change proposals.
--
-- HARD INVARIANT: the model never modifies its own system prompt. LifeSight
-- writes personalization_summaries (evidence) and prompt_change_proposals
-- (always status='pending'). It never writes the user_prompt_overrides table —
-- Oliver's admin project owns that write after a human review.
-- See docs/PERSONALIZATION_PROPOSALS_V1_CONTRACT.md.

-- ---------------------------------------------------------------------------
-- Hierarchical summaries (raw conversations -> daily -> multi_day -> weekly)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS personalization_summaries (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    scope                  TEXT NOT NULL,
    period_start           DATE NOT NULL,
    period_end             DATE NOT NULL,
    summary                TEXT NOT NULL,
    source_conversation_ids UUID[] NOT NULL DEFAULT '{}',
    source_summary_ids      UUID[] NOT NULL DEFAULT '{}',
    model_identifier       TEXT NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT personalization_summaries_scope_chk CHECK (
        scope IN ('daily', 'multi_day', 'weekly')
    ),
    CONSTRAINT personalization_summaries_period_chk CHECK (
        period_end >= period_start
    ),
    CONSTRAINT personalization_summaries_summary_nonempty CHECK (
        char_length(btrim(summary)) > 0
    ),
    CONSTRAINT personalization_summaries_period_uidx UNIQUE (
        user_id, scope, period_start, period_end
    )
);

CREATE INDEX IF NOT EXISTS personalization_summaries_user_scope_start_idx
    ON personalization_summaries (user_id, scope, period_start DESC);

COMMENT ON TABLE personalization_summaries IS
    'Hierarchical personalization evidence: daily summaries are built from raw '
    'conversations, multi_day/weekly are rolled up from lower-scope summaries. '
    'EVIDENCE ONLY — never injected into a chat system prompt.';
COMMENT ON COLUMN personalization_summaries.scope IS
    'daily | multi_day | weekly. daily reads raw conversations; rollups read '
    'lower-scope summary rows.';
COMMENT ON COLUMN personalization_summaries.source_conversation_ids IS
    'Conversations actually read to build this row (populated for scope=daily). '
    'Never fabricated — empty for rollups, whose chain is source_summary_ids.';
COMMENT ON COLUMN personalization_summaries.source_summary_ids IS
    'Lower-scope personalization_summaries rows actually read to build this '
    'rollup (empty for scope=daily).';
COMMENT ON COLUMN personalization_summaries.model_identifier IS
    'Model that produced the summary text (PERSONALIZATION_SUMMARY_MODEL).';

-- ---------------------------------------------------------------------------
-- Prompt change proposals (AI proposes, human approves, admin applies)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prompt_change_proposals (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id               UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    mode                  TEXT,
    proposed_instructions TEXT NOT NULL,
    final_instructions    TEXT,
    reasoning             TEXT NOT NULL,
    evidence              JSONB NOT NULL DEFAULT '{}'::jsonb,
    risks                 TEXT,
    status                TEXT NOT NULL DEFAULT 'pending',
    model_identifier      TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at           TIMESTAMPTZ,
    reviewed_by           TEXT,
    applied_override_id   UUID,
    CONSTRAINT prompt_change_proposals_mode_chk CHECK (
        mode IS NULL
        OR mode IN (
            'fitness',
            'diet',
            'author',
            'brainstorm',
            'mail_calendar',
            'jarvis',
            'checkin'
        )
    ),
    CONSTRAINT prompt_change_proposals_status_chk CHECK (
        status IN ('pending', 'approved', 'rejected', 'applied')
    ),
    CONSTRAINT prompt_change_proposals_proposed_nonempty CHECK (
        char_length(btrim(proposed_instructions)) > 0
    ),
    CONSTRAINT prompt_change_proposals_reasoning_nonempty CHECK (
        char_length(btrim(reasoning)) > 0
    ),
    -- A proposal cannot become approved/applied without a recorded human reviewer.
    CONSTRAINT prompt_change_proposals_reviewer_required_chk CHECK (
        status NOT IN ('approved', 'applied')
        OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL)
    )
);

-- At most ONE pending proposal per (user_id, mode-or-global): two competing
-- pending proposals for the same target can never both exist.
CREATE UNIQUE INDEX IF NOT EXISTS prompt_change_proposals_one_pending_per_user_mode
    ON prompt_change_proposals (user_id, (COALESCE(mode, '')))
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS prompt_change_proposals_user_status_created_idx
    ON prompt_change_proposals (user_id, status, created_at DESC);

COMMENT ON TABLE prompt_change_proposals IS
    'AI-generated per-user prompt change proposals awaiting human review. '
    'LifeSight only ever inserts status=pending rows and never writes the '
    'user_prompt_overrides table — Oliver admin owns approval and application.';
COMMENT ON COLUMN prompt_change_proposals.mode IS
    'NULL = global proposal for all modes; otherwise a MODE_REGISTRY key '
    '(same allowlist as user_prompt_overrides.mode).';
COMMENT ON COLUMN prompt_change_proposals.proposed_instructions IS
    'IMMUTABLE original AI proposal. A trigger rejects any UPDATE that changes '
    'it, so the reviewed artifact always matches what the model produced.';
COMMENT ON COLUMN prompt_change_proposals.final_instructions IS
    'Nullable human-approved/edited instruction text written by admin at review '
    'time. This — not proposed_instructions — is what admin copies into '
    'user_prompt_overrides.instructions.';
COMMENT ON COLUMN prompt_change_proposals.evidence IS
    'JSONB referencing the summary/conversation ids actually read, e.g. '
    '{"source_summary_ids": [...], "source_conversation_ids": [...]}.';
COMMENT ON COLUMN prompt_change_proposals.status IS
    'pending -> approved | rejected -> applied. approved/applied require '
    'reviewed_at and reviewed_by (CHECK enforced).';
COMMENT ON COLUMN prompt_change_proposals.reviewed_by IS
    'Human reviewer actor label (email/username/service), not an FK.';
COMMENT ON COLUMN prompt_change_proposals.applied_override_id IS
    'Soft audit reference to the user_prompt_overrides row admin created. '
    'Intentionally not an FK; this backend never writes this column.';

-- Immutability trigger: proposed_instructions is the AI original. Everything
-- the reviewer needs (final_instructions, status, reviewed_at, reviewed_by,
-- applied_override_id) stays updatable.
CREATE OR REPLACE FUNCTION prompt_change_proposals_freeze_proposed()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.proposed_instructions IS DISTINCT FROM OLD.proposed_instructions THEN
        RAISE EXCEPTION
            'prompt_change_proposals.proposed_instructions is immutable '
            '(proposal id %). Write the reviewed text to final_instructions.',
            OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS prompt_change_proposals_freeze_proposed_trg
    ON prompt_change_proposals;
CREATE TRIGGER prompt_change_proposals_freeze_proposed_trg
    BEFORE UPDATE ON prompt_change_proposals
    FOR EACH ROW
    EXECUTE FUNCTION prompt_change_proposals_freeze_proposed();
