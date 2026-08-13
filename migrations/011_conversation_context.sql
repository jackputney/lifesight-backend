-- 011: conversation titles, rolling summaries, turn-level context metrics.

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS summary_text TEXT,
    ADD COLUMN IF NOT EXISTS summary_through_seq INTEGER;

COMMENT ON COLUMN conversations.title IS
    'V1: first substantive user message truncated (~60 chars), else "{Mode} chat".';
COMMENT ON COLUMN conversations.summary_text IS
    'Rolling summary of older turns; raw messages remain in messages table.';
COMMENT ON COLUMN conversations.summary_through_seq IS
    'Highest message seq included in summary_text (exclusive of recent window).';

CREATE TABLE IF NOT EXISTS conversation_turn_metrics (
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id            UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_tokens               INTEGER,
    output_tokens              INTEGER,
    raw_messages_included      INTEGER NOT NULL DEFAULT 0,
    summary_used               BOOLEAN NOT NULL DEFAULT FALSE,
    summary_through_seq        INTEGER,
    approx_context_utilization REAL,
    supplemental               JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS conversation_turn_metrics_convo_created_idx
    ON conversation_turn_metrics (conversation_id, created_at DESC);

COMMENT ON TABLE conversation_turn_metrics IS
    'Per-model-turn context instrumentation. Never stores system prompt text.';
