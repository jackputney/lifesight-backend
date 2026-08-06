-- 009_fix_mode_check.sql
-- Defensive final-state assertion for the mode CHECK constraints, matching
-- main.py's MODE_REGISTRY (fitness, diet, author, brainstorm, mail_calendar,
-- jarvis) plus 'health' kept for historical rows.
--
-- 004_modes_brainstorm_mail_calendar.sql already sets these same values on
-- main today, so on a linear main history this migration is a no-op (DROP
-- CONSTRAINT IF EXISTS + ADD CONSTRAINT with identical values). It exists as
-- an append-only tail migration per the branch-numbering discussion: with a
-- 005 gap on some trees and multiple in-flight feature branches touching
-- these same three tables, a canonical last-word migration that unconditionally
-- asserts the correct final state is safer than relying on every branch
-- merging in an order where 004's fix is still intact. 003 is left untouched.

ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_mode_check;
ALTER TABLE conversations ADD CONSTRAINT conversations_mode_check
    CHECK (mode IN ('author', 'health', 'jarvis', 'fitness', 'diet', 'brainstorm', 'mail_calendar'));

ALTER TABLE action_log DROP CONSTRAINT IF EXISTS action_log_mode_check;
ALTER TABLE action_log ADD CONSTRAINT action_log_mode_check
    CHECK (mode IN ('author', 'health', 'jarvis', 'fitness', 'diet', 'brainstorm', 'mail_calendar'));

ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS pending_actions_source_mode_check;
ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_source_mode_check
    CHECK (source_mode IN ('author', 'health', 'jarvis', 'fitness', 'diet', 'brainstorm', 'mail_calendar'));
