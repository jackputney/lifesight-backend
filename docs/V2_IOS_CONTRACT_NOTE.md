# v2 iOS contract note (backend → iOS handoff)

## Version ownership (pin against this)

| Role | Commit | Notes |
|------|--------|--------|
| **Backend application runtime** | `d5d150e627351d9759d1ca82e8a50511cadd6f93` | Public `/modes` = fitness/diet/author only; jarvis hidden |
| **This documentation track** | `ff730a8c73f5a6478ed15c187d90224e36fd24ed` | Brainstorm + Mail & Calendar contract approved; **not in runtime yet** |

iOS Home today may still show three modes until slice 2. Pin new five-mode /
`research` decoding to the backend commit that lands slice 1+, not to
`d5d150e` alone.

**Status:** foundation smoke-tested against `613e6e3` / modes pin `d5d150e`.  
**Resolved (product):** five-mode ordered catalog + Brainstorm `research` +
Mail & Calendar Confirm Gate scope — see
[`V2_BRAINSTORM_MAIL_CALENDAR_CONTRACT.md`](./V2_BRAINSTORM_MAIL_CALENDAR_CONTRACT.md).  
**Runtime today:** still three modes until slice 1.  
**Not frozen:** weight units, exercises catalog, Terra units (below).

---

## 1. Public mode list

### Target (approved — not yet runtime)

```json
{"modes":["fitness","diet","author","brainstorm","mail_calendar"]}
```

Order is significant. Clients use this array for enabled modes and Home card
order. No alphabetical re-sort on the server.

| Mode | Visibility | Notes |
|------|------------|--------|
| `fitness` | Active | |
| `diet` | Active | |
| `author` | Active | |
| `brainstorm` | Active (after slice 1) | Global voice research/discussion; not Author plot sessions |
| `mail_calendar` | Active (after slice 1) | Google-first; new `mail_calendar` code only |
| `health` | Retired | |
| `jarvis` | Hidden legacy | Isolated; never advertised; do not reuse for `mail_calendar` |

### Runtime today (`d5d150e`)

```json
{"modes":["author","diet","fitness"]}
```

(alphabetically sorted three-mode list — superseded by the ordered five-mode
target above once slice 1 lands.)

### iOS voice aliases (native — slice 2)

`mail_calendar` must resolve: mail, calendar, email, schedule, “mail and
calendar” / “mail & calendar”. Do not rely on first-word display-name matching
alone. Icons, empty states, a11y labels, and aliases stay on device; `/modes`
drives availability/order.

Full Brainstorm / Mail & Calendar wire rules:
[`V2_BRAINSTORM_MAIL_CALENDAR_CONTRACT.md`](./V2_BRAINSTORM_MAIL_CALENDAR_CONTRACT.md).

---

## 1b. Additive `research` on `/chat` (approved — not yet runtime)

Optional / nullable; separate from `visual_panel`.

```ts
research: {
  status: "not_requested" | "completed" | "failed" | "unavailable",
  query?: string,
  summary?: string,
  uncertainty?: string,
  sources: { title: string, url: string, publisher: string | null, retrieved_at: string }[],
  fact_check: {
    claim: string,
    verdict: "supported" | "partially_supported" | "not_supported" | "inconclusive",
    confidence: number  // 0..1
  } | null
} | null
```

- No `running` until streaming exists.
- No public `snippet` on sources for the initial iOS contract.
- `fact_check` only when `status == "completed"` and a real web search ran.
- Never show “Fact-checked” unless `completed` **and** sources non-empty.
- Do not speak raw URLs through VoiceOver.

Author plot endpoint rename (before global Brainstorm ships):
`POST /author/brainstorm` → `POST /author/brainstorm-session`.

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
| Mail & Calendar send / delete / archive / event mutate / invite / RSVP | **Yes** (when MC ships) |
| Mail & Calendar read / search / draft / free-busy | **No** |
| Brainstorm web research | **No** |

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

---

## 11. Implementation slices (Brainstorm / Mail & Calendar)

See [`V2_BRAINSTORM_MAIL_CALENDAR_CONTRACT.md`](./V2_BRAINSTORM_MAIL_CALENDAR_CONTRACT.md) §6.

| Slice | What |
|------:|------|
| 0 | Contract docs (this track) |
| 1 | Backend empty mode registration + ordered `/modes` + Author endpoint rename + `research: null` on model |
| 2 | iOS five modes + voice aliases |
| 3 | Backend Brainstorm `ResearchProvider` (Anthropic first) |
| 4 | iOS citation / fact-check UI |
| 5 | Backend MC Google OAuth + read tools |
| 6 | Backend MC Confirm Gate writes |
| 7 | iOS MC connection / draft / pending states |

Do not start slices 3 or 5 until slice 0 is reviewed. Do not change `Mode.swift`
until slice 1 contract/runtime registration is approved to proceed.
