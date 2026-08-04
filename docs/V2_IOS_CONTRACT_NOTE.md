# v2 iOS contract note (backend → iOS handoff)

## Version ownership (pin against this)

| Role | Commit | Notes |
|------|--------|--------|
| **Backend application contract** | `613e6e389902ba88fd0be3e2df29ee4a6f9e9a04` | Runtime code that was smoke-tested |
| **This documentation** | `a93b12651d2279cb135b0463fed0bff9b8b2bb47` (refines `d94e1bb`) | Docs-only; runtime still `613e6e3` |

iOS should pin decoding and UX assumptions to **app commit `613e6e3`** until a later app commit + matching contract note supersede it.

**Status:** foundation smoke-tested against `613e6e3`. Not production-ready.  
**Not frozen:** public mode list, weight units, and several schema proposals below still need product/engineering approval.

---

## 1. Public mode list — UNRESOLVED (runtime ≠ recommendation)

### Runtime today (`613e6e3`)

`GET /modes` returns:

```json
{"modes":["author","diet","fitness","jarvis"]}
```

`health` is **not** in `MODE_REGISTRY` (retired from routing).  
`jarvis` **is still advertised** because it remains in `MODE_REGISTRY`.

### Recommendation (not implemented — needs product approval)

| Mode | Proposed client visibility | Notes |
|------|----------------------------|--------|
| `fitness` | Active | New |
| `diet` | Active | New |
| `author` | Active | Postgres chapters/scenes |
| `health` | Hidden / retired | Removed from backend registry |
| `jarvis` | Hidden legacy | Keep `modes/jarvis/` on disk; omit from `/modes` |

Proposed code shape after approval:

```text
PUBLIC_MODE_IDS = ["fitness", "diet", "author"]
```

Until product explicitly approves this, **do not finish iOS navigation/mode cards** as if the recommendation were already live. Treat the mode list as **unresolved**.

---

## 2. `visual_panel` schema

Additive optional field on `ChatResponse` and some domain responses (e.g. `/workouts/voice-log`):

```ts
visual_panel: { type: string, data: object } | null
```

### Client handling

- Decode as optional.
- If `null` / absent → no panel.
- If `type` is **unknown** → **ignore safely** (do not crash); still show `reply` text.

### Supported type today: `workout_sets`

Exact shape returned by `/workouts/voice-log` against `613e6e3`:

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

#### `sets[]` fields (current implementation)

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| `id` | string (UUID) | yes | `set_logs.id` |
| `exercise_id` | string (UUID) | yes | Today: `planned_exercises.id` (unstable across plan re-uploads) |
| `exercise_name` | string | yes | Display name at log time |
| `set_number` | integer | yes | 1-based; array order is meaningful |
| `reps` | integer \| null | yes (nullable) | May be null if parse unclear |
| `weight` | number \| null | yes (nullable) | Decimal allowed |

#### Not in payload yet

| Field | Status |
|-------|--------|
| `unit` / `weight_unit` | **Not sent** |
| `is_pr` | **Not sent** (PR text in `pr_announcements` / `reply`) |
| `load_type` | **Not sent** (see bodyweight rules below) |

---

## 3. Weight / units — do not assume `lb`

### Current (`613e6e3`)

- `"weight": 135` is a bare number.
- **No unit field is sent.**
- Backend engineers may *think* in pounds for gym bars; that is **not** an authoritative API guarantee.

### iOS rules until a unit field ships

1. **Do not** display `"lb"` or `"kg"` as if the server said so.
2. Prefer showing the raw number only, or withhold production-facing weight captions.
3. Do not hardcode a unit in Swift models as required.

### Target shape (not implemented)

```json
"weight": 135.0,
"weight_unit": "lb"
```

Proposed enum: `"lb" | "kg" | null` (`null` only when `weight` is null).

---

## 4. Bodyweight / load semantics

### Current (`613e6e3`)

- Ordinary bodyweight or omitted load → `"weight": null` (not `0`).
- No way to express weighted or assisted bodyweight separately from external load.

### Required distinctions (future contract — document now, implement later)

| Case | Meaning | Future payload sketch |
|------|---------|------------------------|
| Plain bodyweight | e.g. pull-up, no added load | `"load_type": "bodyweight", "weight": null, "weight_unit": null` |
| Weighted bodyweight | bodyweight + external load | `"load_type": "weighted_bodyweight", "weight": 45, "weight_unit": "lb"` |
| Assisted | machine/band assistance | `"load_type": "assistance", "weight": 50, "weight_unit": "lb"` |
| External only | barbell / dumbbell | `"load_type": "external", "weight": 135, "weight_unit": "lb"` |

Until `load_type` exists, iOS must treat `weight: null` as “no external load recorded,” **not** as a fully specified bodyweight taxonomy.

---

## 5. `health_metrics` — add `unit` before production Terra

Columns today: `metric_type`, `value`, `value_json`, `source_device`, `recorded_at` — **no `unit`**.

### Rules (proposal)

| Field | Rule |
|-------|------|
| `value` | Normalized scalar when one number is meaningful; nullable |
| `value_json` | Structured detail; nullable |
| Both set | `value` = primary summary; `value_json` = supplemental |
| `unit` | **Required before production Terra ingestion**, not only before UI — store unambiguous units at write time |

Without units at ingest, kg/lb, m/mi, s/min, mg/dL/mmol/L collisions become permanent.

**Recommendation:** land `ALTER TABLE health_metrics ADD COLUMN unit TEXT` (or equivalent) **before** production wearable traffic. See also note in `docs/proposed/004_stable_exercises_catalog.sql`.

**iOS: do not build health metric UI against the current schema.**

---

## 6. Public JSON names vs DB columns

| Concept | API field |
|---------|-----------|
| Plan day ordering | `sort_order` |
| Session calendar day | `session_date` |
| Food row time (DB) | `logged_at` (not on draft JSON today) |

Food draft JSON: `method`, `matched_food_name`, `calories`, `protein_g`, `carbs_g`, `fat_g`, `confidence`, `raw_input_ref`.

---

## 7. Confirm Gate

| Action | Uses Confirm Gate? |
|--------|--------------------|
| `POST /food/entries` | **Yes** |
| `POST /workouts/voice-log` | **No** — `"pending_action": null` |
| Ordinary scene CRUD | **No** |
| Destructive author (e.g. delete scene) | **Yes** |

### HTTP semantics (verified unless noted)

| Case | HTTP | Body |
|------|------|------|
| Cross-user pending action | **403** | `{"detail":"Pending action does not belong to this user"}` |
| Unknown / malformed / already resolved / expired | **200** | `{"result":"That action is no longer pending."}` |

The 200 collapse is deliberate (VoiceOver-friendly; avoids ID oracle). iOS must not expect 404/409.

### TTL

- Default: **10 minutes** (`PENDING_ACTION_TTL` in `main.py`).
- Enforced on `/confirm` (mark `expired`, no side effects).
- No background sweeper yet.
- **Not integration-tested** yet — see test plan below.

---

## 8. Auth / Terra labels

| Area | Label |
|------|--------|
| `/auth/*` + `AUTH_MODE=real` | Implemented, **not** integration-tested |
| Dev-mode user isolation (`…0001` / `…0002`) | Smoke-tested (Confirm Gate 403) |
| Real Supabase JWT / Apple / magic-link | **Unverified** |
| Terra | **Incomplete** — do not wire production wearable UI |

---

## 9. Pending-action TTL — integration test plan (not yet run)

Goal: prove server-side expiry without relying on the iOS timer.

1. **Short TTL harness** — temporarily override TTL to ~5–10 seconds *in a test-only path*, or insert a `pending_actions` row with `expires_at` in the past / near future via SQL (preferred: no production code change).
2. **Before expiry** — `POST /confirm` `approved: true` on a fresh food pending action → executes (200 Saved).
3. **After expiry** — create pending action; wait or set `expires_at` in the past; `POST /confirm` → **200** `"That action is no longer pending."`; verify **no** `food_entries` row written.
4. **Expired + approve vs reject** — both must be no-ops with the same 200 body.
5. **Cross-user + expired** — other user's expired action → still **403** (ownership before / as well as expiry; document actual order if it differs).
6. **Idempotency** — second confirm after expiry → same 200 body.
7. **No indefinite usability** — expired row `status` is `expired` in DB after the confirm attempt (or equivalent), not left `pending` forever after a confirm touch.

Do not claim TTL proven until this plan has exact status codes/bodies recorded against a known app commit.

---

## 10. Stable exercises catalog

Proposal lives at `docs/proposed/004_stable_exercises_catalog.sql` — **do not apply** until schema review completes. Backfill / identity rules are expanded in that file.
