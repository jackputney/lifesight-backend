# User context + history V1 — iOS contract handoff

Additive backend contracts on branch `feature/user-context-history-v1`.
Existing `/chat` fields remain: `reply`, `mode`, `conversation_id`,
`pending_action`, `visual_panel`, `research`, `client_actions`.

Migrations: `010_user_profiles.sql`, `011_conversation_context.sql`,
`012_profile_onboarding_fields.sql`, `013_personal_context_daily_checkin.sql`.

## 1. `visual_panel` — Fitness `exercise`

```json
{
  "type": "exercise",
  "data": {
    "exercise_id": null,
    "exercise_name": "Bench Press",
    "sets": 4,
    "reps": 8,
    "rest_seconds": 120,
    "current_set": 1,
    "notes": null
  }
}
```

- Emitted from Fitness `/chat` only when Claude calls tool `present_exercise_panel`
  after a concrete exercise prescription is established.
- `exercise_id` is a UUID or `null` — never invented.
- Ordinary fitness chat → `visual_panel: null`.
- `/workouts/voice-log` continues to use `workout_sets` (unchanged this sprint).

## 2. Profile — `GET/PATCH /profile`

Identity remains `/auth/me` (`display_name`, `email`, …).

`GET /profile` / `PATCH /profile` (JWT owner only; no body `user_id`):

| Field | Notes |
|-------|--------|
| timezone | IANA string |
| date_of_birth | date |
| height_cm / weight_kg | numbers |
| preferred_units | `imperial` \| `metric` (nullable) |
| interaction_style | `standard` \| `voice_first` \| `high_accessibility` |
| vision_preference | nullable string |
| spoken_response_preference | nullable string |
| experience_level | nullable string |
| primary_goals | ordered string[] — **index 0 = primary**, 1–2 optional secondary; **max 3**. Supported onboarding values: `build_muscle`, `get_stronger`, `lose_body_fat`, `improve_endurance`, `general_fitness`, `longevity_health`, `track_nutrition`, `return_to_training`, `better_habits`. Legacy stored values may still appear on GET. |
| training_frequency | canonical V1 wire (nullable): `0_1` (0–1 days/week), `2`, `3`, `4`, `5`, `6_plus`. Reuses the same column — no duplicate field. Legacy free-text may still appear on GET. |
| training_environment | `commercial_gym` \| `home_gym` \| `limited_equipment` \| `bodyweight_outdoors` \| `mixed` (nullable) |
| typical_session_minutes | nullable int, **10–300** |
| available_equipment | string[] |
| injuries_limitations | nullable text |
| nutrition_goal | nullable string |
| dietary_preferences | string[] |
| allergies_restrictions | string[] |
| sex_for_physiological_calculations | `male` \| `female` \| `unspecified` (nullable, optional). **Not gender identity** — formula/reference use only. `unspecified` means do not assume male/female. |
| occupation | nullable string, **<=120** — work/role (personal context; not daily state) |
| industry | nullable string, **<=120** |
| education_context | nullable string, **<=240** — school/education |
| interests | string[], **max 20**, each **<=120** |
| typical_schedule | nullable string, **<=500** |

Missing `user_profiles` row → safe nulls / empty arrays. `display_name` may be
echoed for convenience but is stored only on `users`.

There is **no** `onboarding_complete` flag. Backend profile fields are authoritative;
iOS calls `GET /profile` and asks only for missing/null onboarding answers.
Sparse/null profiles remain valid for existing users.

Personal-context fields are permanent identity/context. Daily mood/energy/sleep
belong in `/daily-checkin/*`, not `/profile`.

Chat may update personal-context fields only via the restricted
`update_personal_context` tool (explicit remember/update or answered enrichment
question). Successful writes emit:

```json
{ "type": "refresh_profile" }
```

so iOS can `GET /profile` and sync. Not Confirm Gate. No navigation.

## 3. Chat history

- `GET /conversations?limit=&cursor=`
- `GET /conversations/{id}` → includes **authoritative `mode`**
- `GET /conversations/{id}/messages?limit=&before_seq=`
- Resume: existing `POST /chat` with `conversation_id` (stored mode wins)

Title V1: first substantive user message (~60 chars) else `"{Mode} chat"`.

## 4. `client_actions` — `open_conversation` + `refresh_profile`

```json
{ "type": "open_conversation", "conversation_id": "<uuid>" }
```

```json
{ "type": "refresh_profile" }
```

Not Confirm Gate. Unique match only for reopen; ambiguous → clarify + `[]`.
Navigate V1 unchanged: `{ "type": "navigate", "target": "..." }`.
`refresh_profile` has no payload — client should re-fetch `GET /profile`.

## 5. Daily check-in V1

Dedicated dated state (not `/profile`). Mode `checkin` is chat-acceptable and
hidden from `GET /modes`.

### `GET /daily-checkin/today`

Resolves “today” from the user’s profile `timezone` (fallback `UTC`).

```json
{
  "id": null,
  "user_id": "<uuid>",
  "local_date": "2026-08-13",
  "timezone": "America/Los_Angeles",
  "conversation_id": null,
  "status": "not_started",
  "sleep_hours": null,
  "sleep_quality": null,
  "energy": null,
  "mood": null,
  "stress": null,
  "soreness": null,
  "top_priority": null,
  "notes": null,
  "summary": null,
  "created_at": null,
  "started_at": null,
  "completed_at": null,
  "updated_at": null
}
```

`status`: `not_started` | `in_progress` | `completed`.
Scales `sleep_quality` / `energy` / `mood` / `stress` / `soreness` are 1–5 when set.
`sleep_hours` is 0–24 when set. Not every field is required.

### `POST /daily-checkin/start` (idempotent per user + local date)

- existing `in_progress` → resume (same `conversation_id`)
- existing `completed` → return completed state (no new conversation)
- otherwise create `in_progress` + `mode=checkin` conversation

iOS opens chat with returned `conversation_id` and `mode: "checkin"`.

Structured writes happen via chat tool `update_daily_checkin` (server-side only).
Fitness/Diet prompts receive a **bounded** today’s check-in summary when available.

## 6. Context budget + summaries (server-only)

Env (defaults):

- `CONTEXT_INPUT_TOKEN_BUDGET=24000`
- `CONTEXT_RECENT_MESSAGE_CAP=20`
- `CONTEXT_SUMMARY_THRESHOLD=30`
- `CONTEXT_SUMMARY_TOKEN_BUDGET=1500` (hard cap on stored `summary_text`)
- `CONTEXT_CHARS_PER_TOKEN=4`

Model call = system + compact profile + rolling summary + recent raw window
(capped and trimmed to budget) + current turn. Full raw history stays in Postgres.
Summary failure never fails `/chat`.

## 7. Admin / seed

No public `/admin/*`. Local/staging: `scripts/seed_user_profile.py` with
`ADMIN_SEED_TOKEN`, refuses production unless
`ALLOW_PROFILE_SEED_IN_PRODUCTION=1`. Writes `admin_audit_log`.

Oliver’s separate admin project manages accounts/overrides via Postgres
directly — see `docs/OLIVER_ADMIN_DATABASE_CONTRACT.md` (migration `014`).
LifeSight reads active `user_prompt_overrides` during chat construction and
respects `users.is_active` on every authenticated request.
