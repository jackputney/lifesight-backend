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

| Column | Type | Notes |
|--------|------|--------|
| `id` | UUID PK | `gen_random_uuid()` |
| `user_id` | UUID NOT NULL FK → `users(id)` ON DELETE CASCADE | |
| `scope` | TEXT NOT NULL | `daily` \| `multi_day` \| `weekly` (CHECK) |
| `period_start` | DATE NOT NULL | inclusive, UTC calendar date |
| `period_end` | DATE NOT NULL | inclusive; CHECK `period_end >= period_start` |
| `summary` | TEXT NOT NULL | trimmed non-empty prose; **evidence, not instructions** |
| `source_conversation_ids` | UUID[] NOT NULL DEFAULT `'{}'` | conversations actually read (populated for `daily`) |
| `source_summary_ids` | UUID[] NOT NULL DEFAULT `'{}'` | lower-scope summary rows actually read (populated for rollups) |
| `model_identifier` | TEXT NOT NULL | model that produced the text |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT `now()` | |

Constraints and indexes:

- `UNIQUE (user_id, scope, period_start, period_end)`
  (`personalization_summaries_period_uidx`) — the runner upserts on this, so
  re-running a period overwrites one row instead of accumulating duplicates.
- `INDEX (user_id, scope, period_start DESC)`.

Evidence-chain rules (relied on by audit views):

- `scope = 'daily'` → `source_conversation_ids` non-empty, `source_summary_ids` empty.
- `scope IN ('multi_day','weekly')` → `source_summary_ids` non-empty,
  `source_conversation_ids` empty (a rollup did not read raw conversations,
  so it does not claim them).
- `weekly` prefers `multi_day` rows and falls back to `daily` rows when no
  `multi_day` row exists for the period.
- Ids are only ever recorded for material that was actually included; when
  context bounds drop a source, its id is dropped with it.

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
| `id` | UUID PK | |
| `user_id` | UUID NOT NULL FK → `users(id)` ON DELETE CASCADE | |
| `mode` | TEXT nullable | **NULL = global**; else the same allowlist as `user_prompt_overrides.mode` |
| `proposed_instructions` | TEXT NOT NULL | **IMMUTABLE** original AI proposal (trigger-enforced), ≤ 8000 chars in practice |
| `final_instructions` | TEXT nullable | Human-approved/edited text, written by admin at review time |
| `reasoning` | TEXT NOT NULL | Why the model proposed this |
| `evidence` | JSONB NOT NULL DEFAULT `'{}'` | Ids actually read (see below) |
| `risks` | TEXT nullable | Model-stated risks |
| `status` | TEXT NOT NULL DEFAULT `'pending'` | `pending` \| `approved` \| `rejected` \| `applied` (CHECK) |
| `model_identifier` | TEXT NOT NULL | model that produced the proposal |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT `now()` | |
| `reviewed_at` | TIMESTAMPTZ nullable | Set by admin at review |
| `reviewed_by` | TEXT nullable | Human reviewer label (email/username/service), not an FK |
| `applied_override_id` | UUID nullable | Soft audit reference to the `user_prompt_overrides` row admin created; **no FK**, and the backend never writes it |

Allowed `mode` values — `NULL` or exactly one of:

`fitness` | `diet` | `author` | `brainstorm` | `mail_calendar` | `jarvis` | `checkin`

`evidence` shape written by the backend:

```json
{
  "source_summary_ids": ["a1b2c3d4-2222-4a2b-8c3d-9e0f11223344"],
  "source_conversation_ids": [],
  "period_start": "2026-08-04",
  "period_end": "2026-08-10",
  "source_scope": "weekly"
}
```

Every id in `evidence` is a row the generator actually read. Treat an empty
`source_summary_ids` as a red flag, not a normal proposal.

Example row as inserted by LifeSight:

```json
{
  "id": "c3d4e5f6-4444-4a2b-8c3d-9e0f11223344",
  "user_id": "00000000-0000-4000-8000-000000000001",
  "mode": "fitness",
  "proposed_instructions": "Keep spoken replies to two sentences unless asked for detail.",
  "final_instructions": null,
  "reasoning": "Three weekly summaries show repeated requests for shorter replies.",
  "evidence": { "source_summary_ids": ["a1b2c3d4-2222-4a2b-8c3d-9e0f11223344"] },
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

---

## 5. Status lifecycle — exactly what admin sets

`pending` → `approved` → `applied`, or `pending` → `rejected`.
LifeSight only ever creates `pending`. All other transitions are admin writes.

| Transition | Columns admin must set |
|------------|------------------------|
| `pending` → `rejected` | `status='rejected'`, `reviewed_at=now()`, `reviewed_by=<actor>` |
| `pending` → `approved` | `status='approved'`, `final_instructions=<approved text>`, `reviewed_at=now()`, `reviewed_by=<actor>` |
| `approved` → `applied` | `status='applied'`, `applied_override_id=<new user_prompt_overrides.id>` (keep `reviewed_at` / `reviewed_by`) |

CHECK `prompt_change_proposals_reviewer_required_chk`:

```sql
status NOT IN ('approved','applied')
OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL)
```

A proposal therefore **cannot** become `approved` or `applied` without a
recorded human reviewer — set `reviewed_at` and `reviewed_by` in the same
statement that sets `status`, or the UPDATE fails.

`reviewed_at` / `reviewed_by` are not required for `rejected` at the DB level,
but admin should record them anyway for audit.

Approving and applying may be one transaction (recommended, section 7) or two
steps; the CHECK holds either way.

---

## 6. One pending proposal per target

```sql
CREATE UNIQUE INDEX prompt_change_proposals_one_pending_per_user_mode
    ON prompt_change_proposals (user_id, (COALESCE(mode, '')))
    WHERE status = 'pending';
```

This is the backend’s authoritative answer to “two separate pending proposals
targeting the same `(user_id, mode)`”: the second INSERT raises a unique
violation (SQLSTATE `23505`). It cannot happen, by construction. `mode IS NULL`
(global) is one distinct target via `COALESCE(mode,'')`, so a global proposal
and a `fitness` proposal may both be pending simultaneously.

How admin should handle the conflict cleanly:

1. Treat `23505` on this index as a **normal, expected** outcome, not a 500.
   Surface it as “this user/mode already has a proposal awaiting review.”
2. Resolve the existing pending row first (`approved` or `rejected`). Once it
   leaves `pending` the index no longer covers it and a new proposal can be
   created.
3. Historical rows are unlimited — many `approved` / `rejected` / `applied` rows
   per `(user_id, mode)` are fine.
4. Do not work around the index by inserting with a non-`pending` status.

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

Notes:

- `$4` is the **human-approved** text (`final_instructions`), never
  `proposed_instructions`.
- Step 3 before step 4 matters: `user_prompt_overrides_one_active_per_user_mode`
  (migration `014`) allows only one `is_active` row per
  `(user_id, COALESCE(mode,''))`. If a concurrent transaction wins, the INSERT
  fails with `23505` — retry the whole transaction rather than deleting rows.
- Two reviewers racing on the same proposal: the `FOR UPDATE` in step 1 makes
  the loser observe `status <> 'pending'` and abort.
- The partial unique indexes on both tables are the final safety net. Treat
  their violations as correct behaviour to surface, not errors to suppress.

---

## 8. What admin should **not** do

- Do not modify `proposed_instructions` (the trigger will reject it).
- Do not set `status='approved'` or `'applied'` without `reviewed_at` and
  `reviewed_by` (the CHECK will reject it).
- Do not apply a proposal by writing `proposed_instructions` into
  `user_prompt_overrides.instructions` — apply `final_instructions`.
- Do not write `personalization_summaries`; it is backend-generated evidence.
- Do not expect the backend to approve, apply, expire, or auto-retry anything.
- Do not create a second pending proposal for a target that already has one.

---

## 9. Scope of this PR

Present in this slice: migration `018`, the summary/proposal writer package
(`shared/personalization/`), and the manual runner
`scripts/run_personalization_rollup.py`
(`--user-id`, `--scope {daily,multi_day,weekly}`, `--date` /
`--period-start` / `--period-end`, `--mode`, `--propose`, `--dry-run`).

Not present: any HTTP endpoint for summaries or proposals, any scheduler, any
chat-time read of summaries, and any backend write to `user_prompt_overrides`.
