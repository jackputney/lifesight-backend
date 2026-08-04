# v2 iOS contract note (backend → iOS handoff)

**Against commit:** `613e6e389902ba88fd0be3e2df29ee4a6f9e9a04` (`v2-rebuild`)  
**Status:** foundation smoke-tested against this exact commit. Not production-ready.  
**Do not treat this as frozen until the mode-registry decision below is accepted.**

---

## 1. Authoritative active v2 modes (proposed)

| Mode | Active in v2 client? | Notes |
|------|----------------------|--------|
| `fitness` | **Yes** | New |
| `diet` | **Yes** | New (supersedes half of old `health`) |
| `author` | **Yes** | Postgres chapters/scenes; Google Docs gone |
| `health` | **No** | Retired from backend `MODE_REGISTRY` |
| `jarvis` | **Legacy only** | Code stays on disk; **should not** be shown in v2 UI |

### Current backend behavior (as of this commit)

`GET /modes` returns:

```json
{"modes":["author","diet","fitness","jarvis"]}
```

That still **advertises** `jarvis` because it remains in `MODE_REGISTRY`.  
**Recommendation (not yet implemented):** separate public list from on-disk registry:

```text
PUBLIC_MODE_IDS = ["fitness", "diet", "author"]
```

and have `/modes` return only those. Keep `modes/jarvis/` untouched.

### iOS instruction (product decision needed)

Until product explicitly agrees, frontend and backend are misaligned with older iOS docs that say `author / health / jarvis`.

**Proposed superseding rule for iOS:**

```text
Active v2 modes: fitness, diet, author.
Health is retired.
Jarvis remains legacy code but is not shown in the v2 client.
```

Do not finish iOS navigation/mode cards until this is accepted.

---

## 2. `visual_panel` schema

Additive optional field on `ChatResponse` and on some domain responses (e.g. `/workouts/voice-log`):

```ts
visual_panel: { type: string, data: object } | null
```

### Client handling

- Decode as optional.
- If `null` / absent → no panel.
- If `type` is unknown → **ignore** (do not crash); still show `reply` text.

### Supported type today: `workout_sets`

Exact shape returned by `/workouts/voice-log` against this commit:

```json
{
  "type": "workout_sets",
  "data": {
    "session_id": "648ed59f-26aa-4861-b0c1-edb342616c00",
    "sets": [
      {
        "id": "9fa0914a-fd81-48a3-b876-ac3c9a060658",
        "exercise_id": "b62a6d5f-2b1a-44f6-b0f8-9265f2f25a6f",
        "exercise_name": "Bench Press",
        "set_number": 1,
        "reps": 5,
        "weight": 135
      }
    ]
  }
}
```

#### `sets[]` field contract (current implementation)

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| `id` | string (UUID) | yes | `set_logs.id` |
| `exercise_id` | string (UUID) | yes | Today: `planned_exercises.id` (unstable across plan re-uploads — see exercises proposal) |
| `exercise_name` | string | yes | Display name at log time |
| `set_number` | integer | yes | 1-based; order in array is meaningful (log order) |
| `reps` | integer \| null | yes (nullable) | Parsed from utterance; may be null if unclear |
| `weight` | number \| null | yes (nullable) | **Decimal allowed** (JSON number). Bodyweight / omitted → `null` (not `0`) |

#### Not yet in the payload (gaps — do not invent on iOS)

| Field | Status |
|-------|--------|
| `unit` | **Not sent.** Implicit convention today: pounds (`lb`) for loaded lifts. Must be added before multi-unit support. |
| `is_pr` | **Not sent.** PR text is in `pr_announcements[]` / `reply` only. |

When units are added, expected shape:

```json
"weight": 135.0,
"unit": "lb"
```

Allowed units (proposed): `"lb" | "kg" | null` (`null` only when `weight` is null).

---

## 3. `health_metrics` rules (not ready for iOS display)

Columns today: `metric_type`, `value`, `value_json`, `source_device`, `recorded_at` — **no `unit`**.

### Intended semantics (proposal — not implemented)

| Field | Rule |
|-------|------|
| `value` | Normalized scalar summary when one number is meaningful; nullable |
| `value_json` | Structured detail (sleep stages, HR series, etc.); nullable |
| Both set | `value` is primary summary for lists/tiles; `value_json` is supplemental detail |
| `unit` | **Required for client display** — must be added before iOS renders metrics |
| `metric_type` | Free string (no enum migration); pair with `unit` for display |

Example target record:

```json
{
  "metric_type": "resting_heart_rate",
  "value": 52,
  "unit": "bpm",
  "value_json": null,
  "source_device": "Garmin",
  "recorded_at": "2026-08-04T08:30:00Z"
}
```

**iOS: do not build health metric UI against the current schema.**

---

## 4. Public JSON names vs DB columns

APIs already use the clearer names (not the old spec shorthand):

| Concept | JSON / API field |
|---------|------------------|
| Plan day ordering | `sort_order` |
| Session calendar day | `session_date` |
| Food row time (DB) | `logged_at` (not exposed on draft JSON today) |

Food draft JSON fields: `method`, `matched_food_name`, `calories`, `protein_g`, `carbs_g`, `fat_g`, `confidence`, `raw_input_ref`.

---

## 5. Confirm Gate (what iOS can rely on)

| Action | Uses Confirm Gate? |
|--------|--------------------|
| `POST /food/entries` | **Yes** — returns `pending_action`; save on `/confirm` |
| `POST /workouts/voice-log` | **No** — writes immediately; `pending_action: null` |
| Ordinary scene CRUD | **No** |
| Destructive author (e.g. delete scene) | **Yes** (via pending action) |

Verified against this commit: approve, reject, reconfirm, invalid id, cross-user → 403.

---

## 6. Auth / Terra (labels for iOS planning)

| Area | Label |
|------|--------|
| `/auth/*` + `AUTH_MODE=real` | **Implemented, not integration-tested** |
| Terra connect + webhook | **Incomplete** — do not wire production wearable UI yet |

Authorization logic was smoke-tested in **development mode** (fixed / seeded UUIDs ending `…0001` / `…0002`). That proves user-isolation checks in Confirm Gate code paths. It does **not** prove real Supabase JWT parsing, signature validation, expiry, Apple exchange, or magic-link.

---

## 7. Confirm Gate HTTP semantics (explicit)

### Cross-user
`POST /confirm` on another user's pending action → **HTTP 403**  
```json
{"detail":"Pending action does not belong to this user"}
```

### Unknown / expired / already-resolved (deliberate collapse)
These all return **HTTP 200** with the same spoken-friendly body (no execution):

```json
{"result":"That action is no longer pending."}
```

Applies to:
- nonexistent / malformed action IDs,
- already confirmed or rejected,
- pending rows whose `expires_at` has passed (flipped to `expired` on the confirm attempt).

This is intentional: one VoiceOver-friendly line; avoids revealing whether an ID ever existed. iOS should **not** expect 404/409 for these cases.

### TTL (server-side)
- Default TTL: **10 minutes** (`PENDING_ACTION_TTL` in `main.py`).
- Enforced on `/confirm`: if `status == pending` and `now > expires_at`, the row is marked `expired` and the response is the same “no longer pending” 200 — **no side effects**.
- There is **no background sweeper/cron** yet; expiry is checked at confirm time. Client-side timers are UX only; the server remains the authority.
- TTL expiry was **not** integration-tested in the post-commit suite (approve/reject/duplicate/cross-user were). Treat TTL as implemented-in-code, evidence pending.

### Workout sets vs Confirm Gate
`POST /workouts/voice-log` writes immediately and returns `"pending_action": null`. Proven against `613e6e3`.

---

## 8. Weight / units (current vs required)

| Topic | Current (613e6e3) | Contract direction |
|-------|-------------------|--------------------|
| Loaded weight | JSON number, e.g. `135` or `135.5` | Decimal allowed |
| Bodyweight / omitted | `null` (not `0`) | Keep |
| Unit in payload | **Absent** | Must add `unit`: `"lb" \| "kg" \| null` before multi-unit UI |
| Implicit assumption today | pounds (`lb`) for gym bars | Documented interim only — do not hardcode forever on iOS without `unit` |

Until `unit` ships, iOS may display an interim “lb” label only if product accepts that interim; safer to show the number without a unit caption.

---

## 9. `/modes` / Jarvis (product decision still open)

**As of 613e6e3, `/modes` still exposes `jarvis`.**  
Recommended public registry (not coded yet): `fitness`, `diet`, `author` only.  
`modes/jarvis/` stays on disk. This remains the largest cross-repo blocker.
