# Personalization proposals V1 — Oliver admin database contract

**Audience:** Oliver’s separate admin project (reads/writes Postgres directly).
**Producer:** `lifesight-backend` (writes summaries and *pending* proposals only).
**Migration that freezes this contract:** `migrations/018_personalization.sql`.
Builds on `014_user_prompt_overrides_admin_contract.sql` — read that first, and
`docs/OLIVER_ADMIN_DATABASE_CONTRACT.md` for `users` / `user_prompt_overrides` /
`admin_audit_log`.

There is **no** `/admin/*` HTTP surface and **no** approval endpoint on the
backend. Approval happens entirely in the admin project.

---

## 1. The invariant this schema exists to protect

The model must never silently modify its own system prompt.

- LifeSight writes `personalization_summaries` (evidence) and
  `prompt_change_proposals` rows with `status = 'pending'`.
- LifeSight **never** writes `user_prompt_overrides`. Admin owns that write.
- Summaries are evidence for human review. They are never injected into a chat
  system prompt, and nothing in this slice reads them at chat time.
- Every state transition out of `pending` is performed by admin, with a recorded
  human reviewer.

The database enforces the parts that matter, so a bug in either repo cannot
bypass them: an immutability trigger, a reviewer-required CHECK, and a
one-pending-proposal partial unique index (sections 4–6).

---

## 2. Table: `personalization_summaries`

Hierarchical summaries: raw conversations → `daily` → `multi_day` → `weekly`.
Each level reads only the level below it and records what it actually read.

A note on timestamps before the tables: every `TIMESTAMPTZ` serialized by the
Python helpers (`created_at`, `reviewed_at`) becomes UTC ISO 8601 with a
literal `Z` suffix and **fractional seconds up to 6 digits**, omitted only when
they are exactly zero. The JSON examples below are trimmed for readability and
do not show them, so parse with a formatter that tolerates both rather than a
fixed-millisecond one. `period_start` / `period_end` are plain `DATE` values
(`YYYY-MM-DD`, no time component).

| Column | Type | Notes |
|--------|------|--------|
| `id` | UUID PRIMARY KEY DEFAULT `gen_random_uuid()` | |
| `user_id` | UUID NOT NULL REFERENCES `public.users(id)` ON DELETE CASCADE | |
| `scope` | TEXT NOT NULL | `daily` \| `multi_day` \| `weekly` (CHECK) |
| `period_start` | DATE NOT NULL | inclusive, UTC calendar date |
| `period_end` | DATE NOT NULL | inclusive; CHECK `period_end >= period_start` |
| `summary` | TEXT NOT NULL | trimmed non-empty prose; **evidence, not instructions** |
| `source_conversation_ids` | `UUID[]` NOT NULL DEFAULT `'{}'` | conversations actually read (populated for `daily`) |
| `source_summary_ids` | `UUID[]` NOT NULL DEFAULT `'{}'` | lower-scope summary rows actually read (populated for rollups) |
| `model_identifier` | TEXT NOT NULL | model that produced the text |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT `now()` | |

There is no `updated_at` on this table: a re-run overwrites the row in place and
`created_at` keeps the value from the first insert.

Constraints and indexes, by name. Every CHECK below fails with SQLSTATE `23514`
(`check_violation`) and the UNIQUE with `23505` (`unique_violation`):

| Object | Kind | Definition |
|--------|------|------------|
| `personalization_summaries_scope_chk` | CHECK | `scope IN ('daily', 'multi_day', 'weekly')` |
| `personalization_summaries_period_chk` | CHECK | `period_end >= period_start` |
| `personalization_summaries_summary_nonempty` | CHECK | `char_length(btrim(summary)) > 0` |
| `personalization_summaries_period_uidx` | UNIQUE | `(user_id, scope, period_start, period_end)` |
| `personalization_summaries_user_scope_start_idx` | INDEX | `(user_id, scope, period_start DESC)` |

`personalization_summaries_period_uidx` is declared as a *table constraint*, not
a bare index, so the runner can name it directly in
`ON CONFLICT ON CONSTRAINT personalization_summaries_period_uidx` — re-running a
period overwrites `summary`, both `source_*_ids` arrays, and `model_identifier`
on that one row instead of accumulating duplicates.

Evidence-chain rules (relied on by audit views):

- `scope = 'daily'` → `source_conversation_ids` non-empty, `source_summary_ids` empty.
- `scope IN ('multi_day','weekly')` → `source_summary_ids` non-empty,
  `source_conversation_ids` empty (a rollup did not read raw conversations,
  so it does not claim them).
- `weekly` prefers `multi_day` rows and falls back to `daily` rows when no
  `multi_day` row exists for the period.
- Ids are only ever recorded for material that was actually included; when
  context bounds drop a source, its id is dropped with it.

Those four rules are **conventions upheld by the writer**, not CHECK
constraints. The database would accept a `daily` row with `source_summary_ids`
populated; only the writer guarantees it never happens.

Example row:

```json
{
  "id": "6f2a1d0c-1111-4a2b-8c3d-9e0f11223344",
  "user_id": "00000000-0000-4000-8000-000000000001",
  "scope": "weekly",
  "period_start": "2026-08-04",
  "period_end": "2026-08-10",
  "summary": "Consistently trains three mornings a week and asks for shorter spoken replies.",
  "source_conversation_ids": [],
  "source_summary_ids": [
    "a1b2c3d4-2222-4a2b-8c3d-9e0f11223344",
    "b2c3d4e5-3333-4a2b-8c3d-9e0f11223344"
  ],
  "model_identifier": "claude-haiku-4-5",
  "created_at": "2026-08-11T09:15:00Z"
}
```

Admin may read this table freely. Admin should not write it.

---

## 3. Table: `prompt_change_proposals`

| Column | Type | Notes |
|--------|------|--------|
| `id` | UUID PRIMARY KEY DEFAULT `gen_random_uuid()` | |
| `user_id` | UUID NOT NULL REFERENCES `public.users(id)` ON DELETE CASCADE | |
| `mode` | TEXT nullable | **NULL = global**; else the same allowlist as `user_prompt_overrides.mode` |
| `proposed_instructions` | TEXT NOT NULL | **IMMUTABLE** original AI proposal (trigger-enforced). Non-empty is CHECK-enforced; the 8000-char cap is **not** — see below |
| `final_instructions` | TEXT nullable | Human-approved/edited text, written by admin at review time. No CHECK at all: not length- or emptiness-validated here |
| `reasoning` | TEXT NOT NULL | Why the model proposed this; non-empty CHECK-enforced |
| `evidence` | JSONB NOT NULL DEFAULT `'{}'::jsonb` | Ids actually read (see below). No shape CHECK — the writer owns the shape |
| `risks` | TEXT nullable | Model-stated risks; `NULL` when the model returned none |
| `status` | TEXT NOT NULL DEFAULT `'pending'` | `pending` \| `approved` \| `rejected` \| `applied` (CHECK) |
| `model_identifier` | TEXT NOT NULL | model that produced the proposal |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT `now()` | |
| `reviewed_at` | TIMESTAMPTZ nullable | Set by admin at review |
| `reviewed_by` | TEXT nullable | Human reviewer label (email/username/service), not an FK |
| `applied_override_id` | UUID nullable | Soft audit reference to the `user_prompt_overrides` row admin created; **no FK**, and the backend never writes it |

There is no `updated_at` on this table either. Admin's `reviewed_at` is the
review timestamp; `created_at` never changes.

Constraints, indexes, and the trigger, by name:

| Object | Kind | SQLSTATE | Definition |
|--------|------|----------|------------|
| `prompt_change_proposals_mode_chk` | CHECK | `23514` | `mode IS NULL OR mode IN (<allowlist below>)` |
| `prompt_change_proposals_status_chk` | CHECK | `23514` | `status IN ('pending', 'approved', 'rejected', 'applied')` |
| `prompt_change_proposals_proposed_nonempty` | CHECK | `23514` | `char_length(btrim(proposed_instructions)) > 0` |
| `prompt_change_proposals_reasoning_nonempty` | CHECK | `23514` | `char_length(btrim(reasoning)) > 0` |
| `prompt_change_proposals_reviewer_required_chk` | CHECK | `23514` | see section 5 |
| `prompt_change_proposals_one_pending_per_user_mode` | partial UNIQUE INDEX | `23505` | see section 6 |
| `prompt_change_proposals_user_status_created_idx` | INDEX | — | `(user_id, status, created_at DESC)` — the review-queue read path |
| `prompt_change_proposals_freeze_proposed_trg` | BEFORE UPDATE trigger | `23001` | see section 4 (function `prompt_change_proposals_freeze_proposed`) |

**Length caps — read this before writing `final_instructions`.**
`prompt_change_proposals` has **no** length CHECK on either instruction column.
The backend caps `proposed_instructions` at 8000 characters in application code
(`MAX_PROPOSED_INSTRUCTIONS_CHARS`; longer model output is rejected and nothing
is stored), but the database itself would accept longer text from any other
writer. `user_prompt_overrides.instructions` *does* have a DB CHECK
(`user_prompt_overrides_instructions_maxlen`, `char_length(instructions) <= 8000`),
so an edited `final_instructions` over 8000 characters stores fine here and then
fails with `23514` at apply time in section 7. Validate length at review time.

Allowed `mode` values — `NULL` or exactly one of:

`fitness` | `diet` | `author` | `brainstorm` | `mail_calendar` | `jarvis` | `checkin`

That list is `MODE_REGISTRY` in `main.py`, and it is identical to migration
`014`'s `user_prompt_overrides_mode_chk` allowlist — so an approved proposal can
always be written to an override carrying the same `mode` value. Anything else
fails with `23514` on `prompt_change_proposals_mode_chk`; note in particular that
`health` is retired and is **not** accepted.

`evidence` shape written by the backend — all five keys are always present:

```json
{
  "source_summary_ids": ["a1b2c3d4-2222-4a2b-8c3d-9e0f11223344"],
  "source_conversation_ids": [],
  "period_start": "2026-08-04",
  "period_end": "2026-08-10",
  "source_scope": "weekly"
}
```

- `source_summary_ids` — the `personalization_summaries` rows the generator read.
- `source_conversation_ids` — the conversation ids those summary rows themselves
  claim, carried up so the chain is inspectable from the proposal alone. Empty
  when the sources were `weekly` rollups, which claim no conversations of their
  own (section 2).
- `period_start` / `period_end` — the requested period, as `YYYY-MM-DD` strings.
- `source_scope` — `weekly` or `multi_day`: which scope actually supplied the
  evidence. The generator prefers `weekly` and falls back to `multi_day`.

Every id in `evidence` is a row the generator actually read. Treat an empty
`source_summary_ids` as a red flag, not a normal proposal: the backend returns
without writing anything when it finds no summaries, so a stored proposal always
has at least one.

Example row as inserted by LifeSight. Note `evidence` is the full five-key shape
above, never a subset:

```json
{
  "id": "c3d4e5f6-4444-4a2b-8c3d-9e0f11223344",
  "user_id": "00000000-0000-4000-8000-000000000001",
  "mode": "fitness",
  "proposed_instructions": "Keep spoken replies to two sentences unless asked for detail.",
  "final_instructions": null,
  "reasoning": "Three weekly summaries show repeated requests for shorter replies.",
  "evidence": {
    "source_summary_ids": ["a1b2c3d4-2222-4a2b-8c3d-9e0f11223344"],
    "source_conversation_ids": [],
    "period_start": "2026-08-04",
    "period_end": "2026-08-10",
    "source_scope": "weekly"
  },
  "risks": "Two weeks of evidence may be too thin to generalize.",
  "status": "pending",
  "model_identifier": "claude-haiku-4-5",
  "created_at": "2026-08-11T09:16:00Z",
  "reviewed_at": null,
  "reviewed_by": null,
  "applied_override_id": null
}
```

---

## 4. `proposed_instructions` vs `final_instructions`

- `proposed_instructions` is the **AI original**. A `BEFORE UPDATE` trigger
  (`prompt_change_proposals_freeze_proposed_trg`) raises an exception if an
  UPDATE changes it, so the reviewed artifact always matches what the model
  produced. Do not try to “clean up” this column — the UPDATE will fail.
- `final_instructions` is the **human-approved/edited** text. It starts `NULL`
  and admin writes it at approval time (copy `proposed_instructions` verbatim if
  no edits were needed).
- `final_instructions` is what admin copies into
  `user_prompt_overrides.instructions`. Never apply `proposed_instructions`
  directly.
- `final_instructions`, `status`, `reviewed_at`, `reviewed_by`, and
  `applied_override_id` all remain freely updatable.

Trigger behaviour on violation:

```sql
UPDATE prompt_change_proposals SET proposed_instructions = 'edited' WHERE id = $1;
-- ERROR: prompt_change_proposals.proposed_instructions is immutable (proposal id …).
--        Write the reviewed text to final_instructions.
-- SQLSTATE 23001 (restrict_violation)
```

Exact trigger semantics, so admin can predict it:

- It is `BEFORE UPDATE ... FOR EACH ROW`, so it runs on **every** UPDATE to this
  table, but raises only when the new `proposed_instructions`
  `IS DISTINCT FROM` the old one. Re-assigning the identical value is allowed
  and is a no-op.
- INSERT is unaffected — immutability starts once the row exists.
- `23001` is not one of the usual constraint SQLSTATEs. Match it (or the
  trigger's message) explicitly; a driver that only special-cases `23505` and
  `23514` will surface this as an unknown database error.
- The message interpolates the proposal id only, so it is safe to log — it never
  contains instruction text.

---

## 5. Status lifecycle — exactly what admin sets

`pending` → `approved` → `applied`, or `pending` → `rejected`.
LifeSight only ever creates `pending` — it passes the literal `'pending'`, never
a caller-supplied status. All other transitions are admin writes.

Below, `DB` marks a column the database will reject you for omitting, and
`contract` marks one this document requires but the database does not check:

| Transition | Columns admin must set |
|------------|------------------------|
| `pending` → `rejected` | `status='rejected'` (DB: enum only), `reviewed_at=now()` (contract), `reviewed_by=<actor>` (contract) |
| `pending` → `approved` | `status='approved'` (DB), `reviewed_at=now()` (DB), `reviewed_by=<actor>` (DB), `final_instructions=<approved text>` (contract) |
| `approved` → `applied` | `status='applied'` (DB), `reviewed_at` / `reviewed_by` still non-NULL (DB — keep the existing values), `applied_override_id=<new user_prompt_overrides.id>` (contract) |

CHECK `prompt_change_proposals_reviewer_required_chk`:

```sql
status NOT IN ('approved', 'applied')
OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL)
```

A proposal therefore **cannot** become `approved` or `applied` without a
recorded human reviewer — set `reviewed_at` and `reviewed_by` in the same
statement that sets `status`, or the UPDATE fails with SQLSTATE `23514` naming
`prompt_change_proposals_reviewer_required_chk`. The same CHECK also blocks
*clearing* the reviewer afterwards: setting `reviewed_by = NULL` on an already
`approved` row fails identically.

`reviewed_at` / `reviewed_by` are not required for `rejected` at the DB level,
but admin should record them anyway for audit.

`final_instructions` is likewise **not** DB-required for `approved`. The database
will let you approve with it still `NULL`, and section 7 would then try to write
that `NULL` into `user_prompt_overrides.instructions` and fail on its NOT NULL
(`23502`). Admin must enforce "approved implies `final_instructions` present" in
its own code.

Three more things the database does **not** enforce, so admin must:

- **Ordering.** Nothing rejects `pending` → `applied` directly, or `applied` →
  `rejected`, or re-reviewing a resolved row. Only the enum and the reviewer
  CHECK apply. Gate ordering in admin code (section 7, step 2).
- **Returning to `pending`.** Setting a resolved row back to `pending` is legal
  per-row, but it re-enters the partial unique index of section 6 and fails with
  `23505` if another pending proposal already targets that `(user_id, mode)`.
  Don't reopen rows — create a new proposal instead.
- **Exactness of the status string.** `'in_review'`, or `'applied '` with a
  trailing space, fails with `23514` on `prompt_change_proposals_status_chk`. The
  four values are exact and case-sensitive.

Approving and applying may be one transaction (recommended, section 7) or two
steps; the CHECK holds either way.

---

## 6. One pending proposal per target

```sql
CREATE UNIQUE INDEX IF NOT EXISTS prompt_change_proposals_one_pending_per_user_mode
    ON prompt_change_proposals (user_id, (COALESCE(mode, '')))
    WHERE status = 'pending';
```

This is the backend’s authoritative answer to “two separate pending proposals
targeting the same `(user_id, mode)`”: the second INSERT raises a unique
violation (SQLSTATE `23505`). It cannot happen, by construction. `mode IS NULL`
(global) is one distinct target via `COALESCE(mode,'')`, so a global proposal
and a `fitness` proposal may both be pending simultaneously — and note the
consequence of that expression: a mode literally named `''` would collide with
global, which is one more reason the empty string is not in the allowlist.

### Exactly what the error looks like

```
ERROR:  duplicate key value violates unique constraint
        "prompt_change_proposals_one_pending_per_user_mode"
DETAIL:  Key (user_id, COALESCE(mode, ''::text))=(…, fitness) already exists.
SQLSTATE: 23505
```

Match on the **constraint name**, not the SQLSTATE alone. This index and
`user_prompt_overrides_one_active_per_user_mode` (migration `014`) both raise
`23505` and mean entirely different things, and section 7 can hit either one.

For reference, this is how the backend itself handles it: `insert_pending_proposal`
catches the unique violation and raises a typed `PendingProposalExistsError`
carrying `user_id` and `mode`, whose message is “A pending prompt change proposal
already exists for this user and mode; resolve it before creating another.” The
runner turns that into one line on stderr and exit code `2` — not a stack trace.
Mirror that shape.

### How admin should handle the conflict cleanly

1. Treat `23505` on this index as a **normal, expected** outcome, not a 500.
   Surface it as “this user/mode already has a proposal awaiting review.”
2. Do not pre-check with `SELECT ... WHERE status='pending'` and then INSERT —
   that is racy, and the index is what actually decides. Either catch the error,
   or let Postgres absorb it by naming the partial index's own predicate:

```sql
INSERT INTO prompt_change_proposals (
  user_id, mode, proposed_instructions, reasoning, evidence, model_identifier
)
VALUES ($1, $2, $3, $4, $5::jsonb, $6)
ON CONFLICT (user_id, (COALESCE(mode, ''))) WHERE status = 'pending'
DO NOTHING
RETURNING id;
```

   Zero returned rows then means “a pending proposal already exists for this
   target”, with no exception at all. The `WHERE status = 'pending'` clause is
   required — without it Postgres cannot match the partial index and errors out.
3. Resolve the existing pending row first (`approved` or `rejected`). Once it
   leaves `pending` the index no longer covers it and a new proposal can be
   created.
4. Historical rows are unlimited — many `approved` / `rejected` / `applied` rows
   per `(user_id, mode)` are fine, and several `applied` rows for one target are
   normal over time.
5. Do not work around the index by inserting with a non-`pending` status, and do
   not `DROP` it. It is the only thing stopping two competing prompt rewrites
   from each being reviewed as though it were the sole candidate.

The review queue is simply:

```sql
SELECT id, user_id, mode, proposed_instructions, reasoning, evidence, risks,
       model_identifier, created_at
FROM prompt_change_proposals
WHERE status = 'pending'
ORDER BY created_at ASC;
```

---

## 7. Recommended transactional approval pattern

The backend never writes `user_prompt_overrides` — admin owns that write
entirely. Do approval and application in **one** transaction so concurrent
reviewers converge on exactly one applied proposal and exactly one active
override per `(user_id, mode)`.

```sql
BEGIN;

-- 1. Lock the proposal; serializes concurrent reviewers on this row.
SELECT id, user_id, mode, status
FROM prompt_change_proposals
WHERE id = $1
FOR UPDATE;

-- 2. Bail out if another reviewer already resolved it (status <> 'pending').

-- 3. Deactivate the current active override for this target.
UPDATE user_prompt_overrides
SET is_active = false, updated_at = now()
WHERE user_id = $2
  AND mode IS NOT DISTINCT FROM $3
  AND is_active;

-- 4. Insert the new active override from the HUMAN-approved text.
INSERT INTO user_prompt_overrides (
  user_id, mode, instructions, version, is_active, reason, created_by
)
SELECT $2, $3, $4,
       COALESCE(MAX(version), 0) + 1, true,
       'approved prompt change proposal ' || $1::text, $5
FROM user_prompt_overrides
WHERE user_id = $2 AND mode IS NOT DISTINCT FROM $3
RETURNING id;

-- 5. Record the human review and the applied link on the proposal.
UPDATE prompt_change_proposals
SET status = 'applied',
    final_instructions = $4,
    reviewed_at = now(),
    reviewed_by = $5,
    applied_override_id = $6
WHERE id = $1;

-- 6. Audit.
INSERT INTO admin_audit_log (actor, action, target_user_id, detail)
VALUES (
  $5,
  'upsert_prompt_override',
  $2,
  '{"source":"prompt_change_proposal","changed_fields":["instructions","is_active"]}'::jsonb
);

COMMIT;
```

Parameters: `$1` proposal id, `$2` `user_id`, `$3` `mode` (may be `NULL`), `$4`
the approved text, `$5` the reviewer actor, `$6` the `id` returned by step 4.

Notes:

- `$4` is the **human-approved** text (`final_instructions`), never
  `proposed_instructions`. It must be non-NULL, non-blank, and ≤ 8000 characters:
  `user_prompt_overrides` enforces all three (`instructions` NOT NULL,
  `user_prompt_overrides_instructions_nonempty`,
  `user_prompt_overrides_instructions_maxlen`), and the last two are *not*
  enforced on `prompt_change_proposals.final_instructions`. A blank or too-long
  edit therefore saves fine in section 5 and fails here.
- `mode IS NOT DISTINCT FROM $3` in steps 3 and 4, not `mode = $3`: the global
  target has `mode IS NULL`, and `=` would match nothing.
- Step 1's `FOR UPDATE` locks the proposal row only. It does not lock
  `user_prompt_overrides`, so steps 3–4 still race a concurrent transaction
  applying a *different* proposal for the same target; the index in the next note
  is what resolves that.
- Step 3 before step 4 matters: `user_prompt_overrides_one_active_per_user_mode`
  (migration `014`) allows only one `is_active` row per
  `(user_id, COALESCE(mode,''))`. If a concurrent transaction wins, the INSERT
  fails with `23505` — retry the whole transaction rather than deleting rows.
- Step 4's aggregate subquery always returns exactly one row, so
  `COALESCE(MAX(version), 0) + 1` yields `1` for a brand-new lineage and
  satisfies `user_prompt_overrides_version_positive` (`version >= 1`).
  `created_by` is NOT NULL, which is why `$5` is required. `updated_at` defaults
  to `now()` on insert but has no auto-update trigger — that is why step 3 sets
  it explicitly.
- Step 5 updates `prompt_change_proposals` and therefore fires the section 4
  trigger. It does not touch `proposed_instructions`, so the trigger passes. Keep
  it that way: adding `proposed_instructions` to that SET list breaks approval
  with `23001`.
- Two reviewers racing on the same proposal: the `FOR UPDATE` in step 1 makes
  the loser observe `status <> 'pending'` and abort.
- Step 6's `admin_audit_log` columns are `actor`, `action`, `target_user_id`,
  `detail` JSONB, and `created_at` (default `now()`); `id` defaults to
  `gen_random_uuid()`. `target_user_id` is `REFERENCES users(id) ON DELETE SET
  NULL`, so pass the real `user_id`. Keep `detail` to metadata — never the
  instruction text itself.
- The partial unique indexes on both tables are the final safety net. Treat
  their violations as correct behaviour to surface, not errors to suppress.

### SQLSTATE quick reference

Everything admin can hit against these two tables, and what it means:

| SQLSTATE | Name | Raised by | Meaning / handling |
|----------|------|-----------|--------------------|
| `23505` | `unique_violation` | `prompt_change_proposals_one_pending_per_user_mode` | A pending proposal already targets this `(user_id, mode)`. Expected — surface as a message (section 6). |
| `23505` | `unique_violation` | `user_prompt_overrides_one_active_per_user_mode` | A concurrent reviewer already activated an override for this target. Retry the whole transaction (section 7). |
| `23505` | `unique_violation` | `personalization_summaries_period_uidx` | That user/scope/period summary already exists. The backend upserts; admin should not be writing this table. |
| `23514` | `check_violation` | `prompt_change_proposals_reviewer_required_chk` | Tried to approve/apply without both `reviewed_at` and `reviewed_by`. Fix the statement. |
| `23514` | `check_violation` | `prompt_change_proposals_status_chk` | The status string is not one of the four exact values. |
| `23514` | `check_violation` | `prompt_change_proposals_mode_chk` | `mode` is neither `NULL` nor in the allowlist (e.g. retired `health`). |
| `23514` | `check_violation` | `user_prompt_overrides_instructions_maxlen` / `_nonempty` | The approved text is over 8000 characters, or blank. Validate at review time. |
| `23502` | `not_null_violation` | `user_prompt_overrides.instructions` / `.created_by` | Approved with `final_instructions` still `NULL`, or no reviewer actor passed. |
| `23001` | `restrict_violation` | `prompt_change_proposals_freeze_proposed_trg` | Tried to change `proposed_instructions`. Write to `final_instructions` (section 4). |
| `23503` | `foreign_key_violation` | `prompt_change_proposals_user_id_fkey` | The `user_id` does not exist. Deleting a user CASCADEs both of these tables away. |

---

## 8. What admin should **not** do

- Do not modify `proposed_instructions` (the trigger will reject it).
- Do not set `status='approved'` or `'applied'` without `reviewed_at` and
  `reviewed_by` (the CHECK will reject it).
- Do not set `status='approved'` with `final_instructions` still `NULL`. The
  database allows it; applying that row does not.
- Do not apply a proposal by writing `proposed_instructions` into
  `user_prompt_overrides.instructions` — apply `final_instructions`.
- Do not assume the database enforces transition ordering, or a length cap on
  either instruction column here. It enforces neither (sections 3 and 5).
- Do not write `personalization_summaries`; it is backend-generated evidence.
- Do not treat a summary as an instruction. Nothing in these tables is a system
  prompt until a human copies `final_instructions` into `user_prompt_overrides`.
- Do not expect the backend to approve, apply, expire, or auto-retry anything.
- Do not create a second pending proposal for a target that already has one, and
  do not reopen a resolved proposal back to `pending`.

---

## 9. Scope of this PR

Present in this slice: migration `018`, the summary/proposal writer package
(`shared/personalization/`), and the manual runner
`scripts/run_personalization_rollup.py`
(`--user-id`, `--scope {daily,multi_day,weekly}`, `--date` /
`--period-start` / `--period-end`, `--mode`, `--propose`, `--dry-run`).

Not present: any HTTP endpoint for summaries or proposals, any scheduler, any
chat-time read of summaries, and any backend write to `user_prompt_overrides`.
