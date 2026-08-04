-- 004: allow brainstorm + mail_calendar on mode CHECK constraints
-- Required so /chat can persist conversations for the Slice 1B public modes.
-- Keeps legacy health/jarvis values valid for existing rows.

ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_mode_check;
ALTER TABLE conversations ADD CONSTRAINT conversations_mode_check
    CHECK (mode IN (
        'author', 'health', 'jarvis', 'fitness', 'diet',
        'brainstorm', 'mail_calendar'
    ));

ALTER TABLE action_log DROP CONSTRAINT IF EXISTS action_log_mode_check;
ALTER TABLE action_log ADD CONSTRAINT action_log_mode_check
    CHECK (mode IN (
        'author', 'health', 'jarvis', 'fitness', 'diet',
        'brainstorm', 'mail_calendar'
    ));

ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS pending_actions_source_mode_check;
ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_source_mode_check
    CHECK (source_mode IN (
        'author', 'health', 'jarvis', 'fitness', 'diet',
        'brainstorm', 'mail_calendar'
    ));
