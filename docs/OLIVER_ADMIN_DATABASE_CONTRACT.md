# Oliver admin handoff — LifeSight database contract (V1)

**Audience:** Oliver’s separate admin project (manages Postgres directly).  
**Consumer:** `lifesight-backend` (reads these contracts at runtime).  
**Do not invent a parallel prompt-override schema** — this document is the source of truth.

LifeSight does **not** ship `/admin/*` HTTP CRUD. Your admin app writes tables with a privileged DB role.

Branch / migration that freezes this contract:

- `migrations/014_user_prompt_overrides_admin_contract.sql`
- Builds on `013_personal_context_daily_checkin.sql` (personal context + daily check-ins)

---

## 1. Identity & account status

### Table: `users` (existing; migration `006`, metadata in `014`)

| Column | Type | Notes |
|--------|------|--------|
| `id` | UUID PK | LifeSight user id |
| `username` | TEXT UNIQUE | lowercased |
| `email` | TEXT UNIQUE nullable | lowercased |
| `password_hash` | TEXT | Argon2id — **never** rewrite from admin unless you own password reset |
| `display_name` | TEXT nullable | |
| **`is_active`** | BOOLEAN NOT NULL DEFAULT TRUE | **Account enablement source of truth** |
| `status_reason` | TEXT nullable | Admin note for latest enable/disable (`014`) |
| `status_changed_at` | TIMESTAMPTZ nullable | (`014`) |
| `status_changed_by` | TEXT nullable | Actor label, not FK (`014`) |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

### Account-status semantics

- `is_active = true` → normal account.
- `is_active = false` → **disabled**:
  - login fails
  - refresh fails
  - `/auth/me` fails
  - **every** authenticated request rejects the access JWT (`assert_session_active` checks `is_active`)
- Recommended disable procedure:
  1. `UPDATE users SET is_active = false, status_reason = …, status_changed_at = now(), status_changed_by = … WHERE id = …`
  2. `UPDATE auth_sessions SET revoked_at = now() WHERE user_id = … AND revoked_at IS NULL`
  3. Insert `admin_audit_log` row (`action = 'disable_user'`)

There is **no** separate `roles` / admin-role table in LifeSight for end users. Message `role` (`user`/`assistant`) is chat-only, not account privilege.

---

## 2. User prompt overrides (new — `014`)

### Table: `user_prompt_overrides`

| Column | Type | Notes |
|--------|------|--------|
| `id` | UUID PK | |
| `user_id` | UUID FK → `users(id)` ON DELETE CASCADE | |
| `mode` | TEXT nullable | **NULL = global** for all modes |
| `instructions` | TEXT NOT NULL | 1–8000 chars (trimmed nonempty) |
| `version` | INTEGER NOT NULL DEFAULT 1 | `>= 1`; admin-managed lineage |
| `is_active` | BOOLEAN NOT NULL DEFAULT FALSE | Only active rows are loaded |
| `reason` | TEXT nullable | Why this override exists |
| `created_by` | TEXT NOT NULL | Admin actor label |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

### Allowed `mode` values

`NULL` or exactly one of:

`fitness` | `diet` | `author` | `brainstorm` | `mail_calendar` | `jarvis` | `checkin`

(Matches `MODE_REGISTRY`. Public `/modes` omits `jarvis` and `checkin`, but overrides may still target them.)

### Active / version semantics

- Many historical rows may exist per `(user_id, mode)`.
- **At most one** `is_active = true` per `(user_id, COALESCE(mode, ''))` (unique partial index).
- LifeSight load order for a chat in mode `M`:
  1. Active row with `mode IS NULL` (global), if any
  2. Active row with `mode = M`, if any
  3. Both may appear; mode-specific is listed after global in the prompt block
- Inactive rows are ignored entirely.
- To roll forward: insert/update a new version, set it `is_active = true`, set previous active for that `(user_id, mode)` to `false` (same transaction).

### Runtime prompt order (LifeSight)

```
IDENTITY
→ EPISTEMIC_GROUNDING
→ FEASIBILITY_AND_NON_SYCOPHANCY
→ MODE_INSTRUCTIONS
→ USER_SPECIFIC_CUSTOMIZATION   ← from this table (subordinate wrapper)
→ date / profile / daily-check-in context / enrichment policy
```

User customization is wrapped with an explicit server preamble: it **must not** weaken Confirm Gate, epistemic, feasibility, or mode hard rules. Do not try to bypass this with instructions text — the consumer treats conflicts as ignore-the-override.

---

## 3. Admin audit events (reuse — do not invent a second table)

### Table: `admin_audit_log` (existing `010`)

| Column | Type | Notes |
|--------|------|--------|
| `id` | UUID PK | |
| `actor` | TEXT NOT NULL | Who acted |
| `action` | TEXT NOT NULL | Stable verb |
| `target_user_id` | UUID nullable FK → `users` ON DELETE SET NULL | |
| `detail` | JSONB NOT NULL DEFAULT `{}` | Metadata only |
| `created_at` | TIMESTAMPTZ | |

### View: `admin_audit_events`

Compatibility alias (`014`):

```sql
SELECT id, actor, action, target_user_id, detail, created_at
FROM admin_audit_log;
```

Prefer writing to **`admin_audit_log`** (base table). Reading via either name is fine.

### Suggested `action` verbs

| action | When |
|--------|------|
| `disable_user` / `enable_user` | Toggle `users.is_active` |
| `upsert_prompt_override` | Create/activate a prompt override |
| `deactivate_prompt_override` | Clear `is_active` |
| `upsert_user_profile` | Profile seed/admin edits |

### `detail` conventions

Include metadata such as:

```json
{
  "changed_fields": ["is_active", "status_reason"],
  "reason": "abuse report",
  "mode": "fitness",
  "version": 3,
  "before": {"is_active": true},
  "after": {"is_active": false}
}
```

**Do not** store passwords, refresh tokens, full message bodies, or unnecessary PII dumps.

LifeSight seed script `scripts/seed_user_profile.py` already writes this table.

---

## 4. Related tables Oliver may read (not redefine)

| Table | Purpose |
|-------|---------|
| `user_profiles` | Durable personalization (`GET/PATCH /profile`); personal-context fields in `013` |
| `daily_checkins` | Dated mood/recovery state (`013`) — **not** permanent profile |
| `auth_sessions` | Refresh sessions — revoke on disable |
| `conversations` / `messages` | Chat history |
| `google_connections` | Per-user Google link metadata (`015`) — email, scopes, timestamps, `revoked_at` |

### Google connections (`015`)

Oliver may inspect connection **metadata** only (`google_email`, `google_subject`,
`granted_scopes`, `created_at` / `updated_at` / `revoked_at` / `last_refresh_at`).
Oliver must **never** decrypt or require `encrypted_refresh_token`. Do not treat
email as the security identity — `google_subject` is the account anchor.

### Personal-context profile fields (`013`)

`occupation`, `industry`, `education_context`, `interests` (JSONB array), `typical_schedule` — permanent context only.

### Daily check-in (`013`)

One row per `(user_id, local_date)`. Status: `not_started` | `in_progress` | `completed`. Do not merge daily state into `user_profiles`.

---

## 5. Foreign keys / constraints (prompt overrides)

- `user_prompt_overrides.user_id` → `users.id` CASCADE
- `mode` CHECK allowlist (or NULL)
- `instructions` nonempty + `<= 8000`
- `version >= 1`
- Unique partial index: one active per `(user_id, COALESCE(mode,''))`

---

## 6. What Oliver should **not** do

- Do not create a competing `prompt_customizations` / `user_system_prompts` table.
- Do not expect LifeSight `/admin/*` endpoints — there are none for this.
- Do not put shared epistemic/feasibility policy into overrides; those are owned by `shared/epistemic.py`.
- Do not use `is_active = false` alone without revoking sessions if you need immediate logout (runtime JWT check also blocks, but revocation is still best practice).
- Do not decrypt Google refresh tokens; metadata-only for `google_connections`.

---

## 7. Smoke SQL examples

Activate a fitness-only customization:

```sql
BEGIN;
UPDATE user_prompt_overrides
SET is_active = false, updated_at = now()
WHERE user_id = $1 AND mode = 'fitness' AND is_active;

INSERT INTO user_prompt_overrides (
  user_id, mode, instructions, version, is_active, reason, created_by
) VALUES (
  $1, 'fitness', 'Prefer concise rest cues.', 1, true,
  'coach preference', 'oliver@admin'
);

INSERT INTO admin_audit_log (actor, action, target_user_id, detail)
VALUES (
  'oliver@admin',
  'upsert_prompt_override',
  $1,
  '{"mode":"fitness","version":1,"changed_fields":["instructions","is_active"]}'::jsonb
);
COMMIT;
```

Disable account:

```sql
BEGIN;
UPDATE users
SET is_active = false,
    status_reason = 'terms violation',
    status_changed_at = now(),
    status_changed_by = 'oliver@admin',
    updated_at = now()
WHERE id = $1;

UPDATE auth_sessions
SET revoked_at = now()
WHERE user_id = $1 AND revoked_at IS NULL;

INSERT INTO admin_audit_log (actor, action, target_user_id, detail)
VALUES (
  'oliver@admin',
  'disable_user',
  $1,
  '{"changed_fields":["is_active"],"reason":"terms violation"}'::jsonb
);
COMMIT;
```
