-- 009: align mode CHECK constraints with main.MODE_REGISTRY (append-only).
-- Do NOT edit 003/004 in place — those already ran in other environments.
--
-- MODE_REGISTRY keys (source of truth in main.py):
--   fitness, diet, author, brainstorm, mail_calendar, jarvis
-- Retired 'health' is remapped before the tighter CHECK is applied so
-- historical rows do not block the constraint change.

-- Remap retired health → fitness (public API no longer accepts health).
UPDATE conversations SET mode = 'fitness' WHERE mode = 'health';
UPDATE action_log SET mode = 'fitness' WHERE mode = 'health';
UPDATE pending_actions SET source_mode = 'fitness' WHERE source_mode = 'health';

ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_mode_check;
ALTER TABLE conversations ADD CONSTRAINT conversations_mode_check
    CHECK (mode IN (
        'fitness', 'diet', 'author', 'brainstorm', 'mail_calendar', 'jarvis'
    ));

ALTER TABLE action_log DROP CONSTRAINT IF EXISTS action_log_mode_check;
ALTER TABLE action_log ADD CONSTRAINT action_log_mode_check
    CHECK (mode IN (
        'fitness', 'diet', 'author', 'brainstorm', 'mail_calendar', 'jarvis'
    ));

ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS pending_actions_source_mode_check;
ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_source_mode_check
    CHECK (source_mode IN (
        'fitness', 'diet', 'author', 'brainstorm', 'mail_calendar', 'jarvis'
    ));
