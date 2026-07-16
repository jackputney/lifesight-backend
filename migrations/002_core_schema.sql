-- 002_core_schema.sql
-- Core LifeSite schema: shared conversation history + action log, the shared
-- confirm-gate, Jarvis memory/reminders, Google OAuth creds, health, writing.
--
-- Identity (frozen): Supabase owns auth; every user_id UUID FKs auth.users(id).
--
-- Sync policy (frozen):
--   * health_entries and general log rows: last-write-wins + soft delete.
--   * WRITING IS NOT LWW. Google Docs is the source of truth; writing_drafts
--     are append-only per offline session and merged into Docs by INSERT
--     (batchUpdate insertText at a saved anchor) — never a full-doc overwrite.
--     See writing_sessions / writing_drafts below.

-- Shared conversation history across all three modes ------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    device_id       TEXT,
    mode            TEXT NOT NULL CHECK (mode IN ('author', 'health', 'jarvis')),
    title           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_message_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_id, mode);

CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content_json    JSONB NOT NULL,          -- Anthropic content blocks: text / tool_use / tool_result
    seq             INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, seq)
);

-- Every tool execution (read or write), all modes ---------------------------
CREATE TABLE IF NOT EXISTS action_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('author', 'health', 'jarvis')),
    tool_name       TEXT NOT NULL,
    args_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary  TEXT,
    confirmed       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_action_log_user ON action_log (user_id, created_at DESC);

-- The shared confirm-gate — ONE table, all modes ----------------------------
CREATE TABLE IF NOT EXISTS pending_actions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    source_mode     TEXT NOT NULL CHECK (source_mode IN ('author', 'health', 'jarvis')),
    action_type     TEXT NOT NULL,          -- 'send_email','create_event','reschedule_event',
                                            -- 'log_meal','log_water','log_weigh_in','insert_manuscript', ...
    payload         JSONB NOT NULL,         -- shape varies per action_type
    description     TEXT NOT NULL,          -- spoken read-back summary
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired')),
    confirmed_via   TEXT CHECK (confirmed_via IN ('voice', 'click')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,   -- a never-resolved voice confirm is a bug; sweep to 'expired'
    resolved_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pending_user_status ON pending_actions (user_id, status);
CREATE INDEX IF NOT EXISTS idx_pending_expiry ON pending_actions (status, expires_at);

-- Jarvis long-term memory ---------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories (user_id);

CREATE TABLE IF NOT EXISTS reminders (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    description    TEXT NOT NULL,
    fire_at        TIMESTAMPTZ NOT NULL,
    condition_json JSONB,                   -- e.g. no-reply chaser: {type,thread_id/from_email,since_iso}
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'fired', 'cancelled')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    fired_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (status, fire_at);

-- Google OAuth (Docs + Gmail + Calendar under one grant) --------------------
CREATE TABLE IF NOT EXISTS oauth_credentials (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL DEFAULT 'google',
    access_token_enc  TEXT,                 -- encrypted at rest (app-level), never plaintext
    refresh_token_enc TEXT,
    scopes            TEXT[],
    expires_at        TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, provider)
);

-- Health --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS health_plans (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    content    TEXT NOT NULL,               -- the plan Health mode cites; it never invents beyond this
    source     TEXT,                        -- upload origin / filename
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS health_entries (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    device_id     TEXT,
    entry_type    TEXT NOT NULL,            -- 'water','meal','weigh_in','workout','supplement', ...
    value_numeric DOUBLE PRECISION,
    value_text    TEXT,
    unit          TEXT,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    note          TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- LWW sync key
    deleted_at    TIMESTAMPTZ                          -- soft delete (never hard-delete synced rows)
);
CREATE INDEX IF NOT EXISTS idx_health_user_time ON health_entries (user_id, recorded_at DESC);

-- Writing — Google Docs is source of truth; these are cache + offline merge --
CREATE TABLE IF NOT EXISTS writing_documents (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    google_doc_id          TEXT,
    title                  TEXT,
    doc_type               TEXT,
    last_known_revision_id TEXT,            -- Docs revisionId, to detect web edits / stale anchors
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),  -- METADATA ONLY — never content LWW
    deleted_at             TIMESTAMPTZ
);

-- One row per offline dictation session. NOT last-write-wins.
CREATE TABLE IF NOT EXISTS writing_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES writing_documents(id) ON DELETE CASCADE,
    device_id     TEXT,
    google_doc_id TEXT,
    anchor        JSONB,                    -- saved insertion point (Docs index / named range) at session start
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'merged', 'discarded')),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    merged_at     TIMESTAMPTZ
);

-- Append-only text deltas within a session. Merged into Docs via batchUpdate
-- insertText at the session anchor — NEVER a full-document overwrite.
CREATE TABLE IF NOT EXISTS writing_drafts (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES writing_sessions(id) ON DELETE CASCADE,
    device_id  TEXT,
    seq        INTEGER NOT NULL,
    text_delta TEXT NOT NULL,               -- dictated chunk, exactly as read back to the user
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

-- Per-device sync high-water marks ------------------------------------------
CREATE TABLE IF NOT EXISTS sync_state (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    device_id      TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    last_synced_at TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (device_id, entity_type)
);
